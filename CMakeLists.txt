cmake_minimum_required(VERSION 3.20)
project(pydaw_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(pybind11 CONFIG REQUIRED)
find_package(Python COMPONENTS Interpreter REQUIRED)

# Default to the ASIO SDK bundled in the repo so plain rebuilds keep the
# ASIO backend; pass -DPYDAW_ASIO_SDK_DIR= (empty) to disable explicitly.
set(PYDAW_ASIO_SDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/engine/third_party/asio"
    CACHE PATH
    "Optional Steinberg ASIO SDK root; enables RtAudio ASIO backend on Windows")

pybind11_add_module(pydaw_engine
    engine/src/audio_cache.cpp
    engine/src/render_pool.cpp
    engine/src/audio_file.cpp
    engine/src/synth.cpp
    engine/src/effects.cpp
    engine/src/mixer.cpp
    engine/src/audio_engine.cpp
    engine/src/vst_host.cpp
    engine/src/vst_bridge.cpp
    engine/src/bindings.cpp
    engine/src/warp/warp_resample.cpp
    engine/src/warp/warp_fft.cpp
    engine/src/warp/warp_ola.cpp
    engine/src/warp/warp_vocoder.cpp
    engine/src/warp/warp_processor.cpp
    engine/src/warp/warp_offline.cpp
    engine/third_party/RtAudio.cpp
    engine/third_party/vst3/pluginterfaces/base/funknown.cpp
    engine/third_party/vst3/pluginterfaces/base/ustring.cpp
    engine/third_party/vst3/pluginterfaces/base/conststringtable.cpp
    engine/src/vst_sdk_iids.cpp
)

target_include_directories(pydaw_engine PRIVATE
    engine/src
    engine/third_party
    engine/third_party/vst3
)

add_executable(pydaw_vst_host
    engine/src/vst_host_cli.cpp
    engine/src/vst_host.cpp
    engine/src/effects.cpp
    engine/src/vst_sdk_iids.cpp
    engine/third_party/vst3/pluginterfaces/base/funknown.cpp
    engine/third_party/vst3/pluginterfaces/base/ustring.cpp
    engine/third_party/vst3/pluginterfaces/base/conststringtable.cpp
)

target_include_directories(pydaw_vst_host PRIVATE
    engine/src
    engine/third_party
    engine/third_party/vst3
)

# Keep double-click/dev runs in sync with native rebuilds. The editable
# scikit-build redirect can otherwise leave Python code in the workspace while
# pydaw_engine/pydaw_vst_host are still loaded from an older installed wheel.
add_custom_command(TARGET pydaw_engine POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory
            "${CMAKE_CURRENT_SOURCE_DIR}/pydaw"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_FILE:pydaw_engine>"
            "${CMAKE_CURRENT_SOURCE_DIR}/pydaw/$<TARGET_FILE_NAME:pydaw_engine>"
    COMMAND "${Python_EXECUTABLE}"
            "${CMAKE_CURRENT_SOURCE_DIR}/pydaw/_sync_native.py"
)

add_custom_command(TARGET pydaw_vst_host POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory
            "${CMAKE_CURRENT_SOURCE_DIR}/pydaw"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_FILE:pydaw_vst_host>"
            "${CMAKE_CURRENT_SOURCE_DIR}/pydaw/$<TARGET_FILE_NAME:pydaw_vst_host>"
    COMMAND "${Python_EXECUTABLE}"
            "${CMAKE_CURRENT_SOURCE_DIR}/pydaw/_sync_native.py"
)

if(WIN32)
    # RtAudio: WASAPI is always available on Windows.
    target_compile_definitions(pydaw_engine PRIVATE __WINDOWS_WASAPI__ NOMINMAX WIN32_LEAN_AND_MEAN)
    target_compile_definitions(pydaw_vst_host PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
    target_link_libraries(pydaw_engine PRIVATE ole32 winmm ksuser mfplat mfuuid wmcodecdspuuid)
    target_link_libraries(pydaw_vst_host PRIVATE ole32)

    if(PYDAW_ASIO_SDK_DIR)
        set(_ASIO_COMMON_DIR "${PYDAW_ASIO_SDK_DIR}/common")
        set(_ASIO_HOST_DIR "${PYDAW_ASIO_SDK_DIR}/host")
        set(_ASIO_HOST_PC_DIR "${PYDAW_ASIO_SDK_DIR}/host/pc")
        set(_RTAUDIO_ASIO_HELPER_DIR "${CMAKE_CURRENT_SOURCE_DIR}/engine/third_party/rtaudio_asio")
        if(NOT EXISTS "${_ASIO_COMMON_DIR}/asio.cpp")
            set(_ASIO_COMMON_DIR "${PYDAW_ASIO_SDK_DIR}/ASIOSDK2.3/common")
            set(_ASIO_HOST_DIR "${PYDAW_ASIO_SDK_DIR}/ASIOSDK2.3/host")
            set(_ASIO_HOST_PC_DIR "${PYDAW_ASIO_SDK_DIR}/ASIOSDK2.3/host/pc")
        endif()
        if(EXISTS "${_ASIO_COMMON_DIR}/asio.cpp"
                AND EXISTS "${_ASIO_HOST_DIR}/asiodrivers.cpp"
                AND EXISTS "${_ASIO_HOST_PC_DIR}/asiolist.cpp"
                AND EXISTS "${_RTAUDIO_ASIO_HELPER_DIR}/iasiothiscallresolver.h")
            target_compile_definitions(pydaw_engine PRIVATE __WINDOWS_ASIO__)
            target_include_directories(pydaw_engine PRIVATE
                "${_ASIO_COMMON_DIR}"
                "${_ASIO_HOST_DIR}"
                "${_ASIO_HOST_PC_DIR}"
                "${_RTAUDIO_ASIO_HELPER_DIR}"
            )
            target_sources(pydaw_engine PRIVATE
                "${_ASIO_COMMON_DIR}/asio.cpp"
                "${_ASIO_HOST_DIR}/asiodrivers.cpp"
                "${_ASIO_HOST_PC_DIR}/asiolist.cpp"
                "${_RTAUDIO_ASIO_HELPER_DIR}/iasiothiscallresolver.cpp"
            )
            target_compile_options(pydaw_engine PRIVATE
                "/FI${_RTAUDIO_ASIO_HELPER_DIR}/asio_compat.h")
            set(_PYDAW_WARP_SOURCES
                engine/src/warp/warp_resample.cpp
                engine/src/warp/warp_fft.cpp
                engine/src/warp/warp_ola.cpp
                engine/src/warp/warp_vocoder.cpp
                engine/src/warp/warp_processor.cpp
                engine/src/warp/warp_offline.cpp)
            set_source_files_properties(${_PYDAW_WARP_SOURCES}
                PROPERTIES COMPILE_OPTIONS "/UIn" "/UOut" "/Uwin")
            message(STATUS "RtAudio ASIO backend enabled: ${PYDAW_ASIO_SDK_DIR}")
        else()
            message(WARNING
                "PYDAW_ASIO_SDK_DIR is set, but ASIO SDK common/host sources or RtAudio ASIO helper were not found; ASIO backend disabled")
        endif()
    endif()
elseif(APPLE)
    target_compile_definitions(pydaw_engine PRIVATE __MACOSX_CORE__)
    target_link_libraries(pydaw_engine PRIVATE "-framework CoreAudio" "-framework CoreFoundation")
else()
    target_compile_definitions(pydaw_engine PRIVATE __LINUX_ALSA__)
    target_link_libraries(pydaw_engine PRIVATE asound pthread)
endif()

install(TARGETS pydaw_engine DESTINATION pydaw)
install(TARGETS pydaw_vst_host DESTINATION pydaw)
