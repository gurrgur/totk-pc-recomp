// Linux shims for the vendored AMD FidelityFX SDK translation units (runtime/third_party/ffx).
//
// The SDK builds against the MSVC and MinGW C runtimes, which supply <string.h>, <wchar.h>, <stdlib.h> and <stdio.h>
// through <windows.h>, MSVC's _countof macro, std::wstring_convert through <codecvt>, and the sizeof-bounded *_s
// string functions. glibc supplies none of those, so tools/build_game.py passes this header to those units with
// -include and the vendored files stay byte-identical to the SDK. Windows keeps its own CRT. FFX_UNUSED repeats the
// definition of ffx_util.h for the units that do not include it.
#pragma once

#include <math.h>   // log2 of ffx_vk.cpp
#include <stdarg.h> // sprintf_s
#include <stdio.h>  // sprintf_s, swprintf_s
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#if defined(__cplusplus)
#include <locale>   // std::wstring_convert; <codecvt> alone does not declare it in libstdc++
#endif

#ifndef _countof
#define _countof(a) (sizeof(a) / sizeof((a)[0]))
#endif

#ifndef FFX_UNUSED
#define FFX_UNUSED(x)               ((void)(x))
#endif

#if defined(__cplusplus)
// The only alignas in the SDK's host code asks for 32-byte alignment of BackendContext_VK::EffectContext
// (ffx_vk.cpp), but CreateBackendContextVK maps that array into its scratch buffer right after arrays it only aligns
// to 4 bytes, so the memory does not have it and clang's aligned vector stores for the members fault where MSVC's
// plain stores do not. Dropping the request gives the members the alignment the layout actually provides.
#define alignas(...)
#endif

#ifndef _WIN32
// The SDK calls the bounded functions the way MSVC's C++ overloads accept them; the plain C forms of glibc plus the
// wrappers below cover every call site. wcstombs_s keeps one byte for the terminator that its callers index the
// result with strcmp.
static inline wchar_t *wcscpy_s(wchar_t *dst, const wchar_t *src) {
    return wcscpy(dst, src);
}
static inline wchar_t *wcscpy_s(wchar_t *dst, size_t count, const wchar_t *src) {
    wcsncpy(dst, src, count - 1);
    dst[count - 1] = 0;
    return dst;
}
static inline char *strcpy_s(char *dst, size_t count, const char *src) {
    strncpy(dst, src, count - 1);
    dst[count - 1] = 0;
    return dst;
}
static inline int sprintf_s(char *dst, size_t count, const char *format, ...) {
    va_list ap;
    va_start(ap, format);
    int n = vsnprintf(dst, count, format, ap);
    va_end(ap);
    return n;
}
static inline int wcstombs_s(size_t *retval, char *dst, size_t count, const wchar_t *src, size_t src_count) {
    (void)src_count;   // every call site passes count
    size_t n = count ? wcstombs(dst, src, count - 1) : 0;
    if (n == (size_t)-1) n = 0;   // unencodable input: an empty name
    if (count) dst[n] = 0;
    if (retval) *retval = count ? n + 1 : 0;
    return 0;
}
#define swprintf_s swprintf
#endif
