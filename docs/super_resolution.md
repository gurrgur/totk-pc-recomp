# Temporal super resolution

**F1 > Screen > Super Resolution** selects one temporal reconstruction method:

| Method | Requirements |
|---|---|
| Off | Original scene rendering and game scaling. |
| AMD FSR 3.1 | Built-in FidelityFX Vulkan backend; no AMD DLL or vendor-specific driver library. |
| NVIDIA DLSS / DLAA | NVIDIA RTX GPU, driver NGX core and the NVIDIA DLSS model beside the executable. |

Changing the method requires a restart because NGX's device extensions must be negotiated before device creation.
The separate quality setting selects Native AA, Quality, Balanced, Performance or Ultra Performance. Native AA
renders at the output size; the upscale modes use approximately 67%, 58%, 50% and 33% of the output width/height.
A selected temporal method overrides `render_scale`. Quality changes apply live on game 1.4.3; earlier versions need
a restart. If a backend fails after rendering starts on an older version, or before 1.4.3's live resize entry point
is available, the game keeps its current scene size and uses its original scaler until restart. It must not report
that the running game was resized merely because a patch-page value changed. This is image reconstruction, not frame generation.

```ini
[display]
super_resolution = fsr3
super_resolution_mode = quality
```

Equivalent environment variables: `TOTK_SUPER_RESOLUTION` and `TOTK_SUPER_RESOLUTION_MODE`. Canonical values are
`off|fsr3|dlss` and `native|quality|balanced|performance|ultra_performance`. The default method is Off.
Legacy `dlaa = 1` selects DLSS when the new method is absent; legacy `dlss = dlaa` maps to Native AA, and other legacy
quality values retain their meaning. Explicit new settings win, including `super_resolution = off`. Existing files
are not rewritten merely by reading them. DLSS model presets remain NVIDIA-specific.

FSR 1's existing output-to-window scaling setting is separate: it resizes the final picture, including the HUD, when
the window size differs from the configured game output. Temporal reconstruction operates on the 3D scene. The
original bloom, tonemap and HUD draws remain; the settings menu is drawn afterward. The game's FXAA toggle also
remains independent; switching it off avoids applying its extra smoothing over temporal reconstruction.

## Shared renderer and backend boundary

`runtime/vk_temporal.c` owns scene/depth selection, viewport jitter, camera history, camera motion and upstream's
per-object motion raster, rejection masks, history resets and output composition. The method is captured at startup;
only one backend can resolve a frame. FSR does not keep the earlier experimental recognized-skeletal renderer.

- `runtime/vk_dlss.c` contains NGX loading, its Windows/Linux parameter ABI, feature creation and evaluation.
- `runtime/vk_fsr3_backend.cpp` contains the FidelityFX context, backend/shared resources and dispatch.
- `runtime/vk_temporal_backend.h` defines the common input/output contract. Motion is previous minus current,
  unjittered render pixels; each backend handles its own API conventions.

The common frame includes the reserved replay submission fence. This is not inferred from the last submitted fence:
async replay can already be recording a later submission. Resource retirement must cover recorded-but-unsubmitted
commands as well as GPU work. A transient backend deferral keeps the original scene and retries with reset history;
a fatal failure disables that backend for the run. Jitter must not continue after a missed temporal resolve.

Native AA resolves into the scene before bloom. Upscaling reuses upstream's output-sized resolve point and sampled
image substitution, so the game's subsequent scaling draw cannot overwrite reconstruction with the jittered scene.
The shared camera/object-motion path and its original conservative exclusions apply to both methods. Missing previous
object state, unsupported draws and transparents do not magically acquire valid independent motion vectors.

## FidelityFX source and distribution

The built-in implementation uses the MIT-licensed FSR 3.1 upscaler in AMD FidelityFX SDK 1.1.4, pinned at
`c6efa6bf7f2027b3ec94f28578bb5965eabb9e55`. The retained subset has no frame-interpolation swapchain, Windows frame-
generation dependencies, samples or unrelated effects. `tools/build_ffx_shaders.py` generates the SDK shader blobs;
ordinary builds use the checked-in blobs and do not download AMD binaries.

`runtime/ffx_compat.h` supplies portability glue for the SDK's MSVC-oriented helpers and scratch-buffer alignment.
The SDK context storage accounts for four-byte `wchar_t` on Linux (`ffx_types.h`). In the Vulkan backend,
`CreateBackendContextVK` and `CreatePipelineVK` unwind partially created Vulkan objects on failure and reject
context-capacity overflow. The buffer-memory-requirements lookup tries the Vulkan 1.1 core entry point before its
KHR alias: a Vulkan 1.3 application need not enable the promoted extension, and its KHR entry point can be null.
The GLSL `rw_luma_history` storage qualifier is corrected from `rgba8` to `rgba16f` to match the component's
`R16G16B16A16_FLOAT` allocations; mismatched storage formats otherwise produce undefined image values. Shader
freshness checks include the GPU headers as well as pass entry points.
These are portability/error-handling and resource-format changes, not changes to the reconstruction algorithm. Retain upstream headers
and `runtime/third_party/ffx/LICENSE.txt`. Both Windows and Linux release tools
include that license in the binary distribution. See `THIRD_PARTY_NOTICES.md` for attribution.

## Checks

Configuration migration and scene ratios: `tests/super_resolution_config_test.py`.
Backend ABI/error/lifetime fixtures: `tests/dlss_backend_test.py`, `tests/fsr3_backend_test.py`.
Shared orchestration: `tests/temporal_flow_test.py`. C/C++ boundary layout: `tests/temporal_backend_abi_test.py`.
Compiled SDK storage formats and shader-header freshness: `tests/ffx_shader_test.py`.
Object-motion shader validity and descriptor replay: `tests/motion_spirv_test.py` and
`tests/motion_descriptor_test.py`. Validation-enabled graphics layout changes bind an immutable empty ordinary
set before restoring real descriptors, clearing stale push metadata observed in the validation layer. This does
not disable validation, push descriptors, descriptor templates or object motion.
The synthetic `--probe-ffx` exercises the vendored SDK without game content; it is not an end-to-end gameplay check.

Gameplay checks must cover Native AA and all upscale modes, Off and unavailable-DLSS fallback, quality changes,
scene/camera/history resets, shader-cache rebuilds and teardown with Vulkan validation. Test with the recorder and
async replay enabled as well as their disabled paths. A passing smoke run does not establish long-session stability
or image quality. NVIDIA hardware and Windows need their own validation; mock NGX tests cannot establish either.

### Integration validation

On Linux/RADV with an RX 9070 XT, object-motion-enabled Vulkan-validation smoke runs passed Native AA at
1920×1080 and 5120×2880, all four upscale modes at 1920×1080 output, Off, and unavailable-DLSS fallback. Each
reported zero validation errors, shader failures and skipped draws. Recorder/replay combinations `(on,on)`,
`(off,off)`, `(on,off)` and `(off,on)` were exercised across these runs. A 15-second gameplay run with scripted
camera/player input changed Native → Quality → Balanced → Performance → Ultra Performance → Native live;
the game recreated its scene targets at every change and completed 4,339 presents without those failures.
Debugger checks confirmed 40 object-motion draws replayed in a checked frame. Existing unused-fragment-output
warnings also occur with Off; storage-image format warnings were fixed, not suppressed.

The configuration, mock NGX, FSR lifecycle/fault-injection, shared-flow, ABI, motion SPIR-V/descriptor and shader
format/freshness tests pass. The real-SDK GPU fixture passes dispatch, readback, context retirement and allocation
failure/retry checks. Clean-config selftest, configuration, services, Vulkan execution and FSR probes pass.
Windows-target syntax checks pass, but Windows/NVIDIA execution, long sessions and quantitative temporal-image
quality comparisons remain unverified. Game-derived captures and logs remain local under ignored `build/`.
