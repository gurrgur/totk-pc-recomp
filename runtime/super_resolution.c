#include <string.h>

#include "cfg.h"
#include "super_resolution.h"

int sr_method(void) {
    const char *method = cfg_value("TOTK_SUPER_RESOLUTION");
    if (!strcmp(method, "fsr3")) return SR_FSR3;
    if (!strcmp(method, "dlss")) return SR_DLSS;
    return SR_OFF;
}

const char *sr_mode(void) {
    const char *mode = cfg_value("TOTK_SUPER_RESOLUTION_MODE");
    static const char *const modes[] = {"native", "quality", "balanced", "performance", "ultra_performance"};
    for (unsigned i = 0; i < sizeof modes / sizeof *modes; i++)
        if (!strcmp(mode, modes[i])) return modes[i];
    return "native";
}
