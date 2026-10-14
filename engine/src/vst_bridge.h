#pragma once

#include "vst_host.h"

#include <memory>
#include <string>

std::shared_ptr<VstPlugin> createVstBridgePlugin(const std::string& path,
                                                 const std::string& uid,
                                                 bool instrument,
                                                 uint32_t sampleRate);
