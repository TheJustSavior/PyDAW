#pragma once

#include <algorithm>
#include <cmath>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace pydaw::warp {

inline void makeHanning(int nWin, std::vector<float>& hann) {
    nWin = std::max(nWin, 2);
    hann.resize(size_t(nWin));
    for (int i = 0; i < nWin; ++i)
        hann[i] = 0.5f * (1.f - std::cos(2.f * float(M_PI) * float(i)
                                         / float(nWin - 1)));
}

}  // namespace pydaw::warp
