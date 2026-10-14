#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

namespace pydaw::vst_protocol {

inline constexpr uint32_t kSharedAudioFrames = 4096;

inline std::string hexEncode(const std::string& data) {
    std::ostringstream out;
    out << std::hex << std::setfill('0');
    for (unsigned char ch : data)
        out << std::setw(2) << int(ch);
    return out.str();
}

inline std::string hexDecode(const std::string& hex) {
    std::string out;
    out.reserve(hex.size() / 2);
    for (size_t i = 0; i + 1 < hex.size(); i += 2) {
        const std::string byte = hex.substr(i, 2);
        out.push_back(static_cast<char>(std::strtoul(byte.c_str(), nullptr, 16)));
    }
    return out;
}

inline std::vector<std::string> splitTabs(const std::string& line) {
    std::vector<std::string> parts;
    size_t start = 0;
    while (true) {
        const size_t pos = line.find('\t', start);
        if (pos == std::string::npos) {
            parts.push_back(line.substr(start));
            break;
        }
        parts.push_back(line.substr(start, pos - start));
        start = pos + 1;
    }
    return parts;
}

inline int parseIntField(const std::string& s, int fallback = 0) {
    try {
        return std::stoi(s);
    } catch (...) {
        return fallback;
    }
}

inline float parseFloatField(const std::string& s, float fallback = 0.0f) {
    try {
        return std::stof(s);
    } catch (...) {
        return fallback;
    }
}

inline double parseDoubleField(const std::string& s, double fallback = 0.0) {
    try {
        return std::stod(s);
    } catch (...) {
        return fallback;
    }
}

inline uint32_t parseUint32Field(const std::string& s,
                                 uint32_t fallback = 0) {
    try {
        const auto value = std::stoull(s);
        if (value > std::numeric_limits<uint32_t>::max()) return fallback;
        return static_cast<uint32_t>(value);
    } catch (...) {
        return fallback;
    }
}

inline int64_t parseInt64Field(const std::string& s, int64_t fallback = 0) {
    try {
        return static_cast<int64_t>(std::stoll(s));
    } catch (...) {
        return fallback;
    }
}

inline size_t parseSizeField(const std::string& s, size_t fallback = 0) {
    try {
        return static_cast<size_t>(std::stoull(s));
    } catch (...) {
        return fallback;
    }
}

inline uintptr_t parseUintPtrField(const std::string& s,
                                   uintptr_t fallback = 0) {
    try {
        const auto value = std::stoull(s);
        if (value > std::numeric_limits<uintptr_t>::max()) return fallback;
        return static_cast<uintptr_t>(value);
    } catch (...) {
        return fallback;
    }
}

inline std::string sanitizeField(std::string s) {
    std::replace(s.begin(), s.end(), '\t', ' ');
    std::replace(s.begin(), s.end(), '\n', ' ');
    std::replace(s.begin(), s.end(), '\r', ' ');
    return s;
}

} // namespace pydaw::vst_protocol
