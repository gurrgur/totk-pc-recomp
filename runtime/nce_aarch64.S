#if RT_NCE
.text
.global rt_nce_tls_offset
.type rt_nce_tls_offset,%function
rt_nce_tls_offset:
    mrs x0, tpidr_el0
    add x1, x0, :tprel_hi12:rt_nce_current
    add x1, x1, :tprel_lo12_nc:rt_nce_current
    sub x0, x1, x0
    ret
.size rt_nce_tls_offset, .-rt_nce_tls_offset

.p2align 4
.global rt_nce_run
.type rt_nce_run,%function
rt_nce_run:
    stp x29, x30, [sp, #-16]!
    stp x19, x20, [sp, #-16]!
    stp x21, x22, [sp, #-16]!
    stp x23, x24, [sp, #-16]!
    stp x25, x26, [sp, #-16]!
    stp x27, x28, [sp, #-16]!
    stp d8, d9, [sp, #-16]!
    stp d10, d11, [sp, #-16]!
    stp d12, d13, [sp, #-16]!
    stp d14, d15, [sp, #-16]!
    stp x0, x18, [sp, #-16]!
    mov x16, sp
    str x16, [x0, #16]
    mrs x16, fpcr
    str x16, [x0, #32]
    mrs x16, fpsr
    str x16, [x0, #40]
    clrex
    ldr x16, [x0, #48]
    ldr x17, [x0]
    b .Lrestore_call
.size rt_nce_run, .-rt_nce_run

.p2align 4
.global rt_nce_boundary
.type rt_nce_boundary,%function
rt_nce_boundary:
    // The island allocated 32 bytes and saved original IP0/IP1 before using either.
    mrs x16, tpidr_el0
    add x16, x16, :tprel_hi12:rt_nce_current
    add x16, x16, :tprel_lo12_nc:rt_nce_current
    ldr x16, [x16]
    cbz x16, .Lbad_frame
    str x17, [x16, #56]
    ldr x17, [x16]
    stp x0, x1, [x17]
    stp x2, x3, [x17, #16]
    stp x4, x5, [x17, #32]
    stp x6, x7, [x17, #48]
    stp x8, x9, [x17, #64]
    stp x10, x11, [x17, #80]
    stp x12, x13, [x17, #96]
    stp x14, x15, [x17, #112]
    ldp x0, x1, [sp]
    stp x0, x1, [x17, #128]
    stp x18, x19, [x17, #144]
    stp x20, x21, [x17, #160]
    stp x22, x23, [x17, #176]
    stp x24, x25, [x17, #192]
    stp x26, x27, [x17, #208]
    stp x28, x29, [x17, #224]
    str x30, [x17, #240]
    add x2, sp, #32
    str x2, [x17, #256]
    ldr x1, [x16, #56]
    str x1, [x17, #264]
    mrs x0, nzcv
    str x0, [x17, #272]
    mrs x0, fpcr
    str x0, [x17, #320]
    mrs x0, fpsr
    str x0, [x17, #328]
    stp q0, q1, [x17, #512]
    stp q2, q3, [x17, #544]
    stp q4, q5, [x17, #576]
    stp q6, q7, [x17, #608]
    stp q8, q9, [x17, #640]
    stp q10, q11, [x17, #672]
    stp q12, q13, [x17, #704]
    stp q14, q15, [x17, #736]
    stp q16, q17, [x17, #768]
    stp q18, q19, [x17, #800]
    stp q20, q21, [x17, #832]
    stp q22, q23, [x17, #864]
    stp q24, q25, [x17, #896]
    stp q26, q27, [x17, #928]
    stp q28, q29, [x17, #960]
    stp q30, q31, [x17, #992]
    msr fpcr, xzr
    msr fpsr, xzr
    ldr x0, [x16, #24]
    mov sp, x0
    stp x16, x30, [sp, #-16]!
    mov x0, x16
    mov x3, x30
    bl rt_nce_service
    ldr x16, [sp]
    cmp x1, #2
    b.eq .Lhost_return
    ldr x17, [x16]
    mov x16, x0
    cmp x1, #1
    b.ne .Lrestore_call
    // Fixed-B epilogues restore both scratch registers at instruction boundaries.
    ldr x0, [x17, #256]
    sub x0, x0, #32
    ldp x2, x3, [x17, #128]
    stp x2, x3, [x0]
    mov sp, x0
    b .Lrestore_state
.Lrestore_call:
    ldr x0, [x17, #256]
    mov sp, x0
.Lrestore_state:
    ldr x0, [x17, #272]
    msr nzcv, x0
    ldr x0, [x17, #320]
    msr fpcr, x0
    ldr x0, [x17, #328]
    msr fpsr, x0
    ldp q0, q1, [x17, #512]
    ldp q2, q3, [x17, #544]
    ldp q4, q5, [x17, #576]
    ldp q6, q7, [x17, #608]
    ldp q8, q9, [x17, #640]
    ldp q10, q11, [x17, #672]
    ldp q12, q13, [x17, #704]
    ldp q14, q15, [x17, #736]
    ldp q16, q17, [x17, #768]
    ldp q18, q19, [x17, #800]
    ldp q20, q21, [x17, #832]
    ldp q22, q23, [x17, #864]
    ldp q24, q25, [x17, #896]
    ldp q26, q27, [x17, #928]
    ldp q28, q29, [x17, #960]
    ldp q30, q31, [x17, #992]
    ldp x0, x1, [x17]
    ldp x2, x3, [x17, #16]
    ldp x4, x5, [x17, #32]
    ldp x6, x7, [x17, #48]
    ldp x8, x9, [x17, #64]
    ldp x10, x11, [x17, #80]
    ldp x12, x13, [x17, #96]
    ldp x14, x15, [x17, #112]
    ldp x18, x19, [x17, #144]
    ldp x20, x21, [x17, #160]
    ldp x22, x23, [x17, #176]
    ldp x24, x25, [x17, #192]
    ldp x26, x27, [x17, #208]
    ldp x28, x29, [x17, #224]
    ldr x30, [x17, #240]
    ldr x17, [x17, #136]
    br x16
.Lhost_return:
    ldr x0, [x16, #32]
    msr fpcr, x0
    ldr x0, [x16, #40]
    msr fpsr, x0
    ldr x0, [x16, #16]
    mov sp, x0
    ldp x0, x18, [sp], #16
    ldp d14, d15, [sp], #16
    ldp d12, d13, [sp], #16
    ldp d10, d11, [sp], #16
    ldp d8, d9, [sp], #16
    ldp x27, x28, [sp], #16
    ldp x25, x26, [sp], #16
    ldp x23, x24, [sp], #16
    ldp x21, x22, [sp], #16
    ldp x19, x20, [sp], #16
    ldp x29, x30, [sp], #16
    ret
.Lbad_frame:
    brk #0x4e43
.size rt_nce_boundary, .-rt_nce_boundary
// Qualification keeps the original constrained word and probes a following guaranteed-consuming store.
.macro host_probe name, wide
.global \name
.type \name,%function
\name:
    stp x19, x30, [sp, #-16]!
    mov x8, x0
    mov x19, x1
    mov x0, x2
.if \wide
    ldaxr x9, [x8]
    cmp x9, x19
    b.ne 1f
    .inst (0xc800fc00 | (31 << 16) | (8 << 5) | 31)
1:  stlxr w10, x0, [x8]
    ldr x0, [x8]
.else
    ldaxr w9, [x8]
    cmp w9, w19
    b.ne 1f
    .inst (0x8800fc00 | (31 << 16) | (8 << 5) | 31)
1:  stlxr w10, w0, [x8]
    ldr w0, [x8]
.endif
    mrs x11, nzcv
    stp x10, x11, [x3]
    ldp x19, x30, [sp], #16
    ret
.size \name, .-\name
.endm
host_probe rt_nce_host_probe64, 1
host_probe rt_nce_host_probe32, 0
.section .note.GNU-stack,"",%progbits
#endif
