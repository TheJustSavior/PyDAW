#pragma once

// ASIO / Windows headers (force-included on Windows builds) define macros
// that collide with innocent DSP identifiers (In, Out, win, ...).
#ifdef _WIN32
#ifdef interface
#undef interface
#endif
#ifdef In
#undef In
#endif
#ifdef Out
#undef Out
#endif
#ifdef IN
#undef IN
#endif
#ifdef OUT
#undef OUT
#endif
#ifdef win
#undef win
#endif
#ifdef WIN
#undef WIN
#endif
#ifdef small
#undef small
#endif
#endif
