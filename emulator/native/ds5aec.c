/* ds5aec.c -- the native core of the microphone echo canceller (ds5emu/aec.py).
 *
 * One call per 10 ms microphone frame, made through ctypes (which releases the
 * GIL for the whole call, so none of this competes with the bridge's threads):
 *
 *   1. LINEAR stages: one speexdsp MDF adaptive filter (vendored in speexdsp/,
 *      BSD-3-Clause) PER RENDER ENDPOINT, `ds5mdf`, with as many reference
 *      channels as the endpoint has (1, or 2 for stereo: mid and side), run in
 *      a chain -- each stage cancels its endpoint's echo from what the previous
 *      stage left. A stage belongs to its endpoint, not to a slot: it is only
 *      run while that endpoint plays, so a silent endpoint costs nothing and
 *      its filter is still converged when it plays again, and adding a second
 *      endpoint never disturbs the first one's filter. Each stage only ever
 *      subtracts an estimate of its echo.
 *      Per frame and frequency bin, wherever the chain left MORE energy than the
 *      microphone had, the microphone's own bin is used: the output can never
 *      be louder than the input in any bin.
 *   2. RESIDUAL stage (`ds5aec`): a spectral suppressor that knows the
 *      references. Per bin it estimates the echo the linear stages left from
 *      two things -- their own echo ESTIMATE (mic - linear output) and the
 *      reference's power itself -- each scaled by a leakage it learns online
 *      while the bin holds only echo, and attenuates the bin by how much of
 *      it that estimate explains: deep (to -30 dB) where only echo is left,
 *      not at all where something the references do not explain (a person in
 *      the room) dominates, and never below the room's own noise level in
 *      that bin, so the room keeps its sound instead of pumping to silence.
 *      The reference-based half is what removes the echo of a speaker the
 *      linear model cannot follow (a laptop speaker at full volume, sound
 *      processing the loopback never sees, the top octaves where a fraction
 *      of a sample of timing error undoes the subtraction); see residual_gains.
 * The residual stage is a 50 %-overlap STFT (sqrt-Hann analysis/synthesis,
 * perfect reconstruction), so the output is delayed by exactly one frame. With
 * `active == 0` the call runs no stage at all: the output is the input delayed
 * by one frame, sample for sample, so the latency never changes and an idle
 * canceller is a bit-exact pass-through. The frame in which `active` changes is
 * cross-faded between the two outputs.
 *
 * Also here: `ds5rs`, speexdsp's resampler, for render endpoints whose mix
 * format is not 48 kHz (loopback.py).
 *
 * Denormals: every entry point that does DSP sets flush-to-zero and
 * denormals-are-zero for its own duration (and restores the caller's MXCSR):
 * digital silence decaying through an IIR or an adaptive filter otherwise
 * runs into denormal arithmetic, which is up to 100x slower on x86.
 */
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "speex/speex_echo.h"
#include "speex/speex_resampler.h"
#include "kiss_fftr.h"

#if defined(_M_X64) || defined(__x86_64__) || defined(_M_IX86) || defined(__i386__)
#include <xmmintrin.h>
#define FTZ_ON(saved) do { saved = _mm_getcsr(); _mm_setcsr(saved | 0x8040); } while (0)
#define FTZ_OFF(saved) _mm_setcsr(saved)
#else
#define FTZ_ON(saved) do { saved = 0; } while (0)
#define FTZ_OFF(saved) do { (void)saved; } while (0)
#endif

#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif

#define DS5AEC_VERSION 3
#define MAX_STAGES 8
#define MAX_CH 2

static void *zalloc(size_t n) { return calloc(1, n); }

API int ds5aec_version(void) { return DS5AEC_VERSION; }

/* ------------------------------------------------------------------------ */
/* one endpoint's linear stage                                               */
/* ------------------------------------------------------------------------ */

typedef struct {
    SpeexEchoState *mdf;
    int frame, nch;
    int16_t *out;
    int16_t *zeros;               /* frame * nch zeros, for ds5mdf_flush */
} ds5mdf;

API void ds5mdf_free(ds5mdf *m)
{
    if (!m) return;
    if (m->mdf) speex_echo_state_destroy(m->mdf);
    free(m->out); free(m->zeros);
    free(m);
}

API ds5mdf *ds5mdf_new(int frame, int tail, int nch, int rate)
{
    ds5mdf *m;
    if (frame <= 0 || frame > 4096 || tail < frame || nch < 1 || nch > MAX_CH || rate <= 0)
        return NULL;
    m = (ds5mdf *)zalloc(sizeof(*m));
    if (!m) return NULL;
    m->frame = frame; m->nch = nch;
    m->mdf = speex_echo_state_init_mc(frame, tail, 1, nch);
    m->out = (int16_t *)zalloc(sizeof(int16_t) * frame);
    m->zeros = (int16_t *)zalloc(sizeof(int16_t) * frame * nch);
    if (!m->mdf || !m->out || !m->zeros) { ds5mdf_free(m); return NULL; }
    speex_echo_ctl(m->mdf, SPEEX_ECHO_SET_SAMPLING_RATE, &rate);
    return m;
}

API void ds5mdf_reset(ds5mdf *m)
{
    if (m && m->mdf) speex_echo_state_reset(m->mdf);
}

/* Run `frames` frames of silence (reference and microphone) through the
 * filter: its reference history becomes silence without its weights being
 * touched (nothing to adapt on). Used when a capture session starts, so the
 * first frames are not convolved with reference audio from before the pause. */
API void ds5mdf_flush(ds5mdf *m, int frames)
{
    unsigned int csr;
    int i;
    if (!m || !m->mdf) return;
    FTZ_ON(csr);
    for (i = 0; i < frames; i++)
        speex_echo_cancellation(m->mdf, m->zeros, m->zeros, m->out);
    FTZ_OFF(csr);
}

/* ------------------------------------------------------------------------ */
/* the chain + residual stage                                                */
/* ------------------------------------------------------------------------ */

/* Residual-stage tunables (ds5aec_param ids). Defaults in ds5aec_new. */
enum {
    P_BETA = 0,      /* over-subtraction of the residual-echo estimate */
    P_Q,             /* quantile the leakage trackers converge to */
    P_STEP_DB,       /* leakage tracker step per frame, dB */
    P_FLOOR_DB,      /* deepest gain */
    P_NFAC,          /* keep the room's noise: never below nfac * noise floor */
    P_HOLD,          /* reference max-hold, frames (<= MAX_HOLD) */
    P_DECAY,         /* reference max-hold decay per frame beyond 2 */
    P_RELEASE,       /* gain recovery per frame towards a higher target */
    P_NRISE_DB,      /* noise-floor tracker rise, dB per second */
    P_FSM,           /* gain smoothing across +-fsm bins */
    P_REF_LO_HZ,     /* below this the reference-based estimate is weighted down... */
    P_REF_LO_DB,     /* ...by this much */
    P_FRZ,           /* a bin learns only while its error is under frz x the estimate */
    P_DTD,           /* a frame learns nothing while more than this share of bins does not */
    P_LMAX_DB,       /* cap of the residual / linear-estimate leakage */
    P_GMAX_DB,       /* the residual / reference leakage stays under -gmax dB */
    P_BYPASS,        /* 1: gains forced to 1 (the linear chain's output, for analysis) */
    P_NBIAS,         /* the noise floor's mean over its tracked minimum */
    P_LEARN_SNR,     /* a bin learns only while its error is this far above the noise */
    P_NSUB,          /* the share of the room's noise taken off the residual estimate */
    P_L_INIT_DB,     /* starting leakages (setting one restarts its tracker) */
    P_G_INIT_DB,
    P_UP_SLOW_DB,    /* the reference leakage's creep per frame where it is far exceeded */
    P_GATE_L_DB,     /* the learning gate's floor, relative to the linear estimate */
    P_FRESH,         /* frames after the PC starts playing in which every bin learns */
    P_COUNT
};

#define MAX_HOLD 8

typedef struct {
    int frame, nfft, nbins, rate;
    kiss_fftr_cfg fwd, inv;
    float *win;                   /* sqrt-Hann, nfft */
    float *e_prev, *r_prev;       /* previous frame of linear output / summed reference */
    float *ola;                   /* overlap-add tail, frame */
    float *pe, *g, *gt;           /* smoothed error power, gains, gain scratch; nbins */
    float *ns;                    /* noise floor (min-statistics on the smoothed error), nbins */
    float *lL, *lG;               /* leakages, 10 log10: residual / linear estimate, residual / reference */
    float *hist;                  /* (MAX_HOLD + 1) x nbins reference power, newest at row `hpos` */
    float *refw;                  /* per-bin weight of the reference-based estimate */
    int hpos, ns_init, fresh;
    kiss_fft_scalar *tb;
    kiss_fft_cpx *E, *X, *R;
    float *m_prev;                /* previous mic frame, float */
    int16_t *mic_prev;
    float *proc;                  /* processed output of this frame, frame */
    int was_active;
    int dtd_lo, dtd_hi;           /* double-talk decision band, in bins */
    float p[P_COUNT];
    float nrise;                  /* per-frame factor from P_NRISE_DB */
} ds5aec;

API void ds5aec_free(ds5aec *s)
{
    if (!s) return;
    free(s->fwd); free(s->inv);
    free(s->win); free(s->e_prev); free(s->r_prev); free(s->ola);
    free(s->pe); free(s->g); free(s->gt); free(s->ns); free(s->lL); free(s->lG);
    free(s->hist); free(s->refw);
    free(s->tb); free(s->E); free(s->X); free(s->R); free(s->m_prev);
    free(s->mic_prev); free(s->proc);
    free(s);
}

static void derive(ds5aec *s)
{
    int k;
    float lo = s->p[P_REF_LO_HZ], w = powf(10.0f, s->p[P_REF_LO_DB] / 10.0f);
    for (k = 0; k < s->nbins; k++)
        s->refw[k] = ((float)k * s->rate / s->nfft >= lo) ? 1.0f : w;
    s->nrise = powf(10.0f, s->p[P_NRISE_DB] / 10.0f * s->frame / s->rate);
    if (s->p[P_HOLD] < 0) s->p[P_HOLD] = 0;
    if (s->p[P_HOLD] > MAX_HOLD) s->p[P_HOLD] = MAX_HOLD;
}

API ds5aec *ds5aec_new(int frame, int rate)
{
    ds5aec *s;
    int i, nb;
    if (frame <= 0 || frame > 4096 || rate <= 0) return NULL;
    s = (ds5aec *)zalloc(sizeof(*s));
    if (!s) return NULL;
    s->frame = frame; s->rate = rate;
    s->nfft = 2 * frame; s->nbins = nb = frame + 1;
    s->fwd = kiss_fftr_alloc(s->nfft, 0, NULL, NULL);
    s->inv = kiss_fftr_alloc(s->nfft, 1, NULL, NULL);
    s->win = (float *)zalloc(sizeof(float) * s->nfft);
    s->e_prev = (float *)zalloc(sizeof(float) * frame);
    s->r_prev = (float *)zalloc(sizeof(float) * frame);
    s->ola = (float *)zalloc(sizeof(float) * frame);
    s->pe = (float *)zalloc(sizeof(float) * nb);
    s->g = (float *)zalloc(sizeof(float) * nb);
    s->gt = (float *)zalloc(sizeof(float) * nb);
    s->ns = (float *)zalloc(sizeof(float) * nb);
    s->lL = (float *)zalloc(sizeof(float) * nb);
    s->lG = (float *)zalloc(sizeof(float) * nb);
    s->refw = (float *)zalloc(sizeof(float) * nb);
    s->hist = (float *)zalloc(sizeof(float) * nb * (MAX_HOLD + 1));
    s->tb = (kiss_fft_scalar *)zalloc(sizeof(kiss_fft_scalar) * s->nfft);
    s->E = (kiss_fft_cpx *)zalloc(sizeof(kiss_fft_cpx) * nb);
    s->X = (kiss_fft_cpx *)zalloc(sizeof(kiss_fft_cpx) * nb);
    s->R = (kiss_fft_cpx *)zalloc(sizeof(kiss_fft_cpx) * nb);
    s->m_prev = (float *)zalloc(sizeof(float) * frame);
    s->mic_prev = (int16_t *)zalloc(sizeof(int16_t) * frame);
    s->proc = (float *)zalloc(sizeof(float) * frame);
    if (!s->fwd || !s->inv || !s->win || !s->e_prev || !s->r_prev || !s->ola || !s->pe
        || !s->g || !s->gt || !s->ns || !s->lL || !s->lG || !s->refw || !s->hist || !s->tb
        || !s->E || !s->X || !s->R || !s->m_prev || !s->mic_prev || !s->proc) {
        ds5aec_free(s);
        return NULL;
    }
    for (i = 0; i < s->nfft; i++)     /* periodic Hann, square-rooted */
        s->win[i] = (float)sqrt(0.5 - 0.5 * cos(2.0 * 3.14159265358979323846 * i / s->nfft));
    for (i = 0; i < nb; i++) s->g[i] = 1.0f;
    s->dtd_lo = (int)(500.0 * s->nfft / rate);
    s->dtd_hi = (int)(10000.0 * s->nfft / rate);
    if (s->dtd_hi >= nb) s->dtd_hi = nb - 1;
    /* MEASURED, scratchpad/aec/r10 (round-9 laptop-speaker dumps replayed, a clean voice mixed
     * into the mic 6 and 15 dB under the echo): see aec.py's module docstring. */
    s->p[P_BETA] = 1.5f;
    s->p[P_Q] = 0.7f;
    s->p[P_STEP_DB] = 0.3f;
    s->p[P_FLOOR_DB] = -30.0f;
    s->p[P_NFAC] = 1.0f;
    s->p[P_HOLD] = 2.0f;
    s->p[P_DECAY] = 0.6f;
    s->p[P_RELEASE] = 0.5f;
    s->p[P_NRISE_DB] = 0.2f;
    s->p[P_FSM] = 1.0f;
    s->p[P_REF_LO_HZ] = 4000.0f;
    s->p[P_REF_LO_DB] = -6.0f;
    s->p[P_FRZ] = 6.0f;
    s->p[P_DTD] = 0.3f;
    s->p[P_LMAX_DB] = 0.0f;
    s->p[P_GMAX_DB] = 20.0f;
    s->p[P_BYPASS] = 0.0f;
    s->p[P_NBIAS] = 3.0f;
    s->p[P_LEARN_SNR] = 2.0f;
    s->p[P_NSUB] = 1.0f;
    /* START HIGH and learn down: a bin learns only while its error is
     * within frz x the estimate, so an estimate that starts far too low
     * would never learn up (round 10: -50 dB took music at full volume
     * 6 s to come down) */
    s->p[P_L_INIT_DB] = -6.0f;
    s->p[P_G_INIT_DB] = -30.0f;
    s->p[P_UP_SLOW_DB] = 0.02f;
    s->p[P_GATE_L_DB] = -100.0f;
    s->p[P_FRESH] = 150.0f;
    for (i = 0; i < nb; i++) { s->lL[i] = s->p[P_L_INIT_DB]; s->lG[i] = s->p[P_G_INIT_DB]; }
    derive(s);
    return s;
}

/* One tunable. Returns 0, or -1 for an unknown id. */
API int ds5aec_param(ds5aec *s, int id, float v)
{
    if (!s || id < 0 || id >= P_COUNT) return -1;
    s->p[id] = v;
    if (id == P_L_INIT_DB || id == P_G_INIT_DB) {
        int k;
        for (k = 0; k < s->nbins; k++) {
            if (id == P_L_INIT_DB) s->lL[k] = v; else s->lG[k] = v;
        }
    }
    derive(s);
    return 0;
}

API float ds5aec_get_param(ds5aec *s, int id)
{
    if (!s || id < 0 || id >= P_COUNT) return -1.0f;
    return s->p[id];
}

/* A new capture session: the residual stage's short-term state and its one
 * frame of delay line start from silence. What it learned about the room --
 * the leakages and the noise floor -- is kept (the linear stages are kept
 * too; they are reset individually with ds5mdf_reset). */
API void ds5aec_reset(ds5aec *s)
{
    int i;
    if (!s) return;
    memset(s->pe, 0, sizeof(float) * s->nbins);
    memset(s->hist, 0, sizeof(float) * s->nbins * (MAX_HOLD + 1));
    for (i = 0; i < s->nbins; i++) s->g[i] = 1.0f;
    memset(s->e_prev, 0, sizeof(float) * s->frame);
    memset(s->r_prev, 0, sizeof(float) * s->frame);
    memset(s->m_prev, 0, sizeof(float) * s->frame);
    memset(s->ola, 0, sizeof(float) * s->frame);
    memset(s->mic_prev, 0, sizeof(int16_t) * s->frame);
    s->was_active = 0;
}

static void stft(ds5aec *s, const float *prev, const float *cur, kiss_fft_cpx *X)
{
    int i, n = s->frame;
    for (i = 0; i < n; i++) {
        s->tb[i] = prev[i] * s->win[i];
        s->tb[n + i] = cur[i] * s->win[n + i];
    }
    kiss_fftr(s->fwd, s->tb, X);
}

static int16_t sat16(float v)
{
    v = floorf(v + 0.5f);
    if (v > 32767.0f) return 32767;
    if (v < -32768.0f) return -32768;
    return (int16_t)v;
}

/* the room's noise floor per bin: follows a falling smoothed power at once and
 * rises slowly (min-statistics), so speech and echo do not lift it */
static void track_noise(ds5aec *s)
{
    int k;
    if (!s->ns_init) {
        for (k = 0; k < s->nbins; k++) s->ns[k] = s->pe[k] + 1.0f;
        s->ns_init = 1;
    }
    for (k = 0; k < s->nbins; k++) {
        float v = s->pe[k] < s->ns[k] ? s->pe[k] : s->ns[k] * s->nrise;
        s->ns[k] = v < 1.0f ? 1.0f : v;
    }
}

#define DB10(x) (4.3429448f * logf(x))          /* 10 log10 */
#define UNDB10(x) expf(0.23025851f * (x))        /* 10^(x/10) */

/* The residual suppressor, per bin k, on the chain's output E, its echo
 * estimate Y = X - E and the summed reference R:
 *
 *   residual echo  res = max( L * |Y|^2 , w * G * S ),   S = max-hold of |R|^2
 *   gain^2         = 1 - beta * res / |E|^2 (smoothed), never below the room's
 *                    own noise in that bin (nfac x its noise floor) nor `floor`
 *
 * L and G are leakages learned online per bin (log-domain quantile trackers,
 * quantile q): L is what the linear stage leaves of its own estimate (its
 * errors scale with the echo: misalignment, a slowly changing path), G what
 * is left relative to the reference itself (the part the linear stage cannot
 * model at all: a nonlinear speaker, an enhancement the loopback never saw).
 * A bin learns only while its error is within frz x the estimate (not near
 * end) and above the room noise, and a frame where most of the band is near
 * end teaches nothing. */
static void residual_gains(ds5aec *s, const kiss_fft_cpx *E, const kiss_fft_cpx *X,
                           const kiss_fft_cpx *R, float *near_out)
{
    int k, d, nb = s->nbins, hold = (int)s->p[P_HOLD];
    float q = s->p[P_Q], step = s->p[P_STEP_DB];
    float up = step * q, dn = step * (1.0f - q);
    float beta = s->p[P_BETA], frz = s->p[P_FRZ], nfac = s->p[P_NFAC];
    float floor2 = UNDB10(s->p[P_FLOOR_DB]);
    float lmax = s->p[P_LMAX_DB], gcap = -s->p[P_GMAX_DB], decay = s->p[P_DECAY];
    float rel = s->p[P_RELEASE], nbias = s->p[P_NBIAS], nsub = s->p[P_NSUB];
    float upslow = s->p[P_UP_SLOW_DB], gate_l = UNDB10(s->p[P_GATE_L_DB]);
    float *h0;
    int nfar = 0, nband = 0, learn = 1, fsm = (int)s->p[P_FSM];
    float *pr = s->gt;                      /* scratch: S per bin, then the raw gains */
    float wd[MAX_HOLD + 1];

    for (d = 0; d <= hold; d++) wd[d] = d <= 2 ? 1.0f : powf(decay, (float)(d - 2));
    s->hpos = (s->hpos + MAX_HOLD) % (MAX_HOLD + 1);
    h0 = s->hist + (size_t)s->hpos * nb;
    for (k = 0; k < nb; k++) h0[k] = R[k].r * R[k].r + R[k].i * R[k].i;
    for (k = 0; k < nb; k++) {
        float m = 0;
        for (d = 0; d <= hold; d++) {
            float v = s->hist[(size_t)((s->hpos + d) % (MAX_HOLD + 1)) * nb + k] * wd[d];
            if (v > m) m = v;
        }
        pr[k] = m;
    }
    /* frame-level double-talk: the share of the band that is far above the estimate */
    if (frz > 0) {
        for (k = s->dtd_lo; k < s->dtd_hi; k++) {
            float ex = X[k].r - E[k].r, ey = X[k].i - E[k].i;
            float py = ex * ex + ey * ey;
            float pe = E[k].r * E[k].r + E[k].i * E[k].i;
            float r0 = UNDB10(s->lL[k]) * py, r1 = s->refw[k] * UNDB10(s->lG[k]) * pr[k];
            if (r1 > r0) r0 = r1;
            if (gate_l * py > r0) r0 = gate_l * py;
            nband++;
            if (pe < frz * r0) nfar++;
        }
        if (s->p[P_DTD] > 0 && nband > 0 && (float)(nband - nfar) / nband > s->p[P_DTD])
            learn = 0;
    }
    if (s->fresh > 0) {
        /* the PC just started playing: what the mic has is its echo (a person talking over the
         * very first second is the exception, and is unlearned in the echo-only seconds after) */
        s->fresh--;
        learn = 2;
    }
    if (near_out) *near_out = learn ? 0.0f : 1.0f;
    for (k = 0; k < nb; k++) {
        float ex = X[k].r - E[k].r, ey = X[k].i - E[k].i;
        float py = ex * ex + ey * ey;
        float pe = E[k].r * E[k].r + E[k].i * E[k].i;
        float S = pr[k], L = UNDB10(s->lL[k]), G = s->refw[k] * UNDB10(s->lG[k]);
        float res, g2, gn2;
        float N = nbias * s->ns[k];
        if (learn) {
            float r0 = L * py, r1 = G * S;
            /* the gate's estimate: never under gate_l x the linear estimate, so a residual that
             * grew (the volume turned up, the speaker driven into distortion) can still be learned */
            float rg = r0 > r1 ? r0 : r1, rl = gate_l * py;
            if (rl > rg) rg = rl;
            if (frz <= 0 || learn == 2 || pe < frz * rg) {
                /* the echo part of the error, not the room's; an error AT the
                 * room's level is evidence too -- that the leakage is small --
                 * wherever the current estimate predicts more than the room */
                float ee = pe - N, lpe;
                int above = pe > s->p[P_LEARN_SNR] * N;
                if (ee < 0.01f * N) ee = 0.01f * N;
                lpe = DB10(ee + 1e-9f);
                if (py > N && (above || r0 > N)) {
                    float r = lpe - DB10(py + 1e-9f);
                    s->lL[k] += r > s->lL[k] ? up : -dn;
                    if (s->lL[k] > lmax) s->lL[k] = lmax;
                }
                if (S > 100.0f && (above || r1 > N)) {
                    float r = lpe - DB10(S + 1e-9f);
                    s->lG[k] += r > s->lG[k] ? up : -dn;
                    if (s->lG[k] > gcap) s->lG[k] = gcap;
                }
                L = UNDB10(s->lL[k]);
                G = s->refw[k] * UNDB10(s->lG[k]);
            } else if (upslow > 0 && S > 100.0f && pe > s->p[P_LEARN_SNR] * N) {
                /* far above the estimate: a person in the room -- or an echo the
                 * estimate has never seen (a tone where the content had nothing,
                 * the volume turned up). Creep up, slowly: a voice gains a few dB
                 * over a sentence and loses it in the next echo-only second */
                s->lG[k] += upslow;
                if (s->lG[k] > gcap) s->lG[k] = gcap;
                G = s->refw[k] * UNDB10(s->lG[k]);
            }
        }
        res = L * py;
        if (G * S > res) res = G * S;
        /* an estimate at the room's level cannot be told from the room: only
         * what it predicts ABOVE the room is taken out */
        res -= nsub * N;
        if (res < 0) res = 0;
        g2 = 1.0f - beta * res / (s->pe[k] + 1e-9f);
        gn2 = nfac * N / (s->pe[k] + 1e-9f);
        if (gn2 > 1.0f) gn2 = 1.0f;
        if (gn2 < floor2) gn2 = floor2;
        if (g2 < gn2) g2 = gn2;
        if (g2 > 1.0f) g2 = 1.0f;
        pr[k] = sqrtf(g2);                    /* S is no longer needed for this bin */
    }
    for (k = 0; k < nb; k++) {
        float g = pr[k];
        if (fsm > 0) {
            int a = k - fsm, b = k + fsm, j;
            float acc = 0;
            for (j = a; j <= b; j++) acc += (j >= 0 && j < nb) ? pr[j] : 0.0f;
            g = acc / (float)(2 * fsm + 1);
        }
        if (g > s->g[k]) g = s->g[k] + rel * (g - s->g[k]);
        s->g[k] = g;
    }
}

/* mic: frame int16. stages[i] / refs[i], i < nstage: the linear stages to run
 * this frame, in order, and each one's reference frame (frame * nch int16,
 * interleaved; channel 0 -- mono, or the mid of a stereo endpoint -- is also
 * what the residual stage's reference-based estimate uses). out: frame int16
 * (one frame late). gains (optional, nbins floats): the residual gains applied
 * this frame. stats (optional, 8 floats): mic power, linear-output power,
 * echo-estimate power, output power, near-end (double-talk) flag, mean gain,
 * active, reserved. stage_stats (optional, 3 * nstage floats): per stage, the
 * power of its input, of its output and of its own echo estimate (input -
 * output). Returns 0, or -1 on bad arguments (nothing is touched then). */
API int ds5aec_process(ds5aec *s, const int16_t *mic, int nstage, ds5mdf *const *stages,
                       const int16_t *const *refs, int16_t *out, int active,
                       float *gains, float *stats, float *stage_stats)
{
    int i, k, j, n;
    float ecur[4096], mcur[4096], rcur[4096];
    double pmic = 0, plin = 0, pest = 0, pout = 0, gsum = 0;
    float near = 0;
    unsigned int csr;
    if (!s || !mic || !out || nstage < 0 || nstage > MAX_STAGES) return -1;
    if (nstage > 0 && (!stages || !refs)) return -1;
    for (j = 0; j < nstage; j++)
        if (!stages[j] || !stages[j]->mdf || !refs[j] || stages[j]->frame != s->frame) return -1;
    n = s->frame;
    FTZ_ON(csr);
    for (i = 0; i < n; i++) {
        pmic += (double)mic[i] * mic[i];
        mcur[i] = (float)mic[i];
    }

    /* the processed path also runs in the frame where it is switched off, so
     * that frame can be cross-faded out of it */
    if (active && !s->was_active) s->fresh = (int)s->p[P_FRESH];
    if (active || s->was_active) {
        const int16_t *cur = mic;
        for (i = 0; i < n; i++) rcur[i] = 0.0f;
        for (j = 0; j < nstage; j++) {
            ds5mdf *m = stages[j];
            double pe = 0, pin = 0, pou = 0;
            speex_echo_cancellation(m->mdf, cur, refs[j], m->out);
            for (i = 0; i < n; i++) {
                float d = (float)cur[i] - (float)m->out[i];
                pe += (double)d * d;
                pin += (double)cur[i] * cur[i];
                pou += (double)m->out[i] * m->out[i];
                rcur[i] += (float)refs[j][i * m->nch];
            }
            if (stage_stats) {
                stage_stats[3 * j] = (float)(pin / n);
                stage_stats[3 * j + 1] = (float)(pou / n);
                stage_stats[3 * j + 2] = (float)(pe / n);
            }
            cur = m->out;
        }
        for (i = 0; i < n; i++) {
            float y = (float)mic[i] - (float)cur[i];
            ecur[i] = (float)cur[i];
            plin += (double)ecur[i] * ecur[i];
            pest += (double)y * y;
        }
        stft(s, s->e_prev, ecur, s->E);
        stft(s, s->m_prev, mcur, s->X);
        stft(s, s->r_prev, rcur, s->R);
        for (k = 0; k < s->nbins; k++) {
            float pe = s->E[k].r * s->E[k].r + s->E[k].i * s->E[k].i;
            float px = s->X[k].r * s->X[k].r + s->X[k].i * s->X[k].i;
            /* never worse than no cancellation: where the subtraction ADDED
             * energy (a filter that is wrong right now), keep the mic's bin */
            if (pe > px) { s->E[k] = s->X[k]; pe = px; }
            s->pe[k] = 0.5f * s->pe[k] + 0.5f * pe;
        }
        track_noise(s);
        if (s->p[P_BYPASS] > 0) {
            for (k = 0; k < s->nbins; k++) s->g[k] = 1.0f;
        } else {
            residual_gains(s, s->E, s->X, s->R, &near);
        }
        for (k = 0; k < s->nbins; k++) {
            float g = s->g[k];
            s->E[k].r *= g; s->E[k].i *= g;
            gsum += g;
            if (gains) gains[k] = g;
        }
        kiss_fftri(s->inv, s->E, s->tb);
        for (i = 0; i < n; i++) {
            s->proc[i] = s->ola[i] + s->tb[i] * s->win[i] / (float)s->nfft;
            s->ola[i] = s->tb[n + i] * s->win[n + i] / (float)s->nfft;
        }
        memcpy(s->e_prev, ecur, sizeof(float) * n);
        memcpy(s->r_prev, rcur, sizeof(float) * n);
    } else {
        if (stage_stats)
            for (j = 0; j < 3 * nstage; j++) stage_stats[j] = 0.0f;
        /* idle: the noise floor keeps following the room (the output does not
         * depend on it) */
        stft(s, s->m_prev, mcur, s->X);
        for (k = 0; k < s->nbins; k++) {
            float px = s->X[k].r * s->X[k].r + s->X[k].i * s->X[k].i;
            s->pe[k] = 0.5f * s->pe[k] + 0.5f * px;
        }
        track_noise(s);
    }
    memcpy(s->m_prev, mcur, sizeof(float) * n);
    if (!active) {
        /* idle: keep the STFT state as if G == 1 on the raw mic, so a later
         * activation reconstructs seamlessly; the output is the raw delay line */
        for (i = 0; i < n; i++) {
            float w2 = s->win[n + i] * s->win[n + i];
            s->ola[i] = (float)mic[i] * w2;
            s->e_prev[i] = (float)mic[i];
            s->r_prev[i] = 0.0f;
        }
        for (k = 0; k < s->nbins; k++) { s->g[k] = 1.0f; if (gains) gains[k] = 1.0f; }
        memset(s->hist, 0, sizeof(float) * s->nbins * (MAX_HOLD + 1));
        gsum = s->nbins;
        near = 0;
    }

    if (active && s->was_active) {
        for (i = 0; i < n; i++) out[i] = sat16(s->proc[i]);
    } else if (!active && !s->was_active) {
        memcpy(out, s->mic_prev, sizeof(int16_t) * n);
    } else {
        /* the frame where the mode changes: cross-fade raw delay line <-> processed */
        for (i = 0; i < n; i++) {
            float a = (float)(i + 1) / (float)n;          /* weight of the new mode */
            float raw = (float)s->mic_prev[i];
            float v = active ? (1.0f - a) * raw + a * s->proc[i]
                             : (1.0f - a) * s->proc[i] + a * raw;
            out[i] = sat16(v);
        }
    }
    for (i = 0; i < n; i++) pout += (double)out[i] * out[i];
    memcpy(s->mic_prev, mic, sizeof(int16_t) * n);
    s->was_active = active;
    if (stats) {
        stats[0] = (float)(pmic / n); stats[1] = (float)(plin / n);
        stats[2] = (float)(pest / n); stats[3] = (float)(pout / n);
        stats[4] = near; stats[5] = (float)(gsum / s->nbins);
        stats[6] = (float)active; stats[7] = 0.0f;
    }
    FTZ_OFF(csr);
    return 0;
}

/* ------------------------------------------------------------------------ */
/* resampler (loopback endpoints whose mix format is not 48 kHz)             */
/* ------------------------------------------------------------------------ */

API void *ds5rs_new(int nch, int in_rate, int out_rate, int quality)
{
    int err = 0;
    SpeexResamplerState *r;
    if (nch < 1 || nch > 8 || in_rate <= 0 || out_rate <= 0) return NULL;
    r = speex_resampler_init((spx_uint32_t)nch, (spx_uint32_t)in_rate,
                             (spx_uint32_t)out_rate, quality, &err);
    if (!r || err) { if (r) speex_resampler_destroy(r); return NULL; }
    speex_resampler_skip_zeros(r);
    return r;
}

API void ds5rs_free(void *r)
{
    if (r) speex_resampler_destroy((SpeexResamplerState *)r);
}

/* in: *in_len frames (interleaved float). out: room for *out_len frames.
 * On return *in_len / *out_len hold the frames consumed / produced. */
API int ds5rs_process(void *r, const float *in, unsigned int *in_len, float *out,
                      unsigned int *out_len)
{
    unsigned int csr;
    int ret;
    if (!r || !in || !in_len || !out || !out_len) return -1;
    FTZ_ON(csr);
    ret = speex_resampler_process_interleaved_float((SpeexResamplerState *)r, in,
                                                    (spx_uint32_t *)in_len, out,
                                                    (spx_uint32_t *)out_len);
    FTZ_OFF(csr);
    return ret;
}
