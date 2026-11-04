// Shared temporal super-resolution selection; method is restart-only, mode is live on game 1.4.3.
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

enum { SR_OFF = 0, SR_FSR3 = 1, SR_DLSS = 2 };
int sr_method(void);
const char *sr_mode(void);   // native|quality|balanced|performance|ultra_performance

#ifdef __cplusplus
}
#endif
