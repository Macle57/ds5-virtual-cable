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
 *   2. RESIDUAL stage (`ds5aec`): a spectral suppressor keyed on the chain's
 *      echo ESTIMATE (mic - linear output). The residual echo in each bin is
 *      taken as `rho` of the estimate's power; bins whose error power is not
 *      well above that are attenuated, down to a floor that is gentle while the
 *      near end (anything the references do not explain) is active and deeper
 *      while only PC audio is present.
 *
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

#define DS5AEC_VERSION 2
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

typedef struct {
    int frame, nfft, nbins, rate;
    kiss_fftr_cfg fwd, inv;
    float *win;                   /* sqrt-Hann, nfft */
    float *e_prev, *y_prev;       /* previous frame of linear output / echo estimate */
    float *ola;                   /* overlap-add tail, frame */
    float *pe, *py, *g;           /* smoothed powers and gains, nbins */
    kiss_fft_scalar *tb;
    kiss_fft_cpx *E, *Y, *X;
    float *m_prev;                /* previous mic frame, float */
    int16_t *mic_prev;
    float *proc;                  /* processed output of this frame, frame */
    int was_active, near_hold;
    int band_lo, band_hi;         /* near-end detection band, in bins */
    /* tunables (ds5aec_set) */
    float rho, over, gmin_near, gmin_far, near_ratio, smooth, release;
    int hold_frames;
} ds5aec;

API void ds5aec_free(ds5aec *s)
{
    if (!s) return;
    free(s->fwd); free(s->inv);
    free(s->win); free(s->e_prev); free(s->y_prev); free(s->ola);
    free(s->pe); free(s->py); free(s->g); free(s->tb); free(s->E); free(s->Y);
    free(s->X); free(s->m_prev);
    free(s->mic_prev); free(s->proc);
    free(s);
}

API ds5aec *ds5aec_new(int frame, int rate)
{
    ds5aec *s;
    int i;
    if (frame <= 0 || frame > 4096 || rate <= 0) return NULL;
    s = (ds5aec *)zalloc(sizeof(*s));
    if (!s) return NULL;
    s->frame = frame; s->rate = rate;
    s->nfft = 2 * frame; s->nbins = frame + 1;
    s->fwd = kiss_fftr_alloc(s->nfft, 0, NULL, NULL);
    s->inv = kiss_fftr_alloc(s->nfft, 1, NULL, NULL);
    s->win = (float *)zalloc(sizeof(float) * s->nfft);
    s->e_prev = (float *)zalloc(sizeof(float) * frame);
    s->y_prev = (float *)zalloc(sizeof(float) * frame);
    s->ola = (float *)zalloc(sizeof(float) * frame);
    s->pe = (float *)zalloc(sizeof(float) * s->nbins);
    s->py = (float *)zalloc(sizeof(float) * s->nbins);
    s->g = (float *)zalloc(sizeof(float) * s->nbins);
    s->tb = (kiss_fft_scalar *)zalloc(sizeof(kiss_fft_scalar) * s->nfft);
    s->E = (kiss_fft_cpx *)zalloc(sizeof(kiss_fft_cpx) * s->nbins);
    s->Y = (kiss_fft_cpx *)zalloc(sizeof(kiss_fft_cpx) * s->nbins);
    s->X = (kiss_fft_cpx *)zalloc(sizeof(kiss_fft_cpx) * s->nbins);
    s->m_prev = (float *)zalloc(sizeof(float) * frame);
    s->mic_prev = (int16_t *)zalloc(sizeof(int16_t) * frame);
    s->proc = (float *)zalloc(sizeof(float) * frame);
    if (!s->fwd || !s->inv || !s->win || !s->e_prev || !s->y_prev || !s->ola
        || !s->pe || !s->py || !s->g || !s->tb || !s->E || !s->Y || !s->X || !s->m_prev
        || !s->mic_prev || !s->proc) {
        ds5aec_free(s);
        return NULL;
    }
    for (i = 0; i < s->nfft; i++)     /* periodic Hann, square-rooted */
        s->win[i] = (float)sqrt(0.5 - 0.5 * cos(2.0 * 3.14159265358979323846 * i / s->nfft));
    for (i = 0; i < s->nbins; i++) s->g[i] = 1.0f;
    s->band_lo = (int)(300.0 * s->nfft / rate);
    s->band_hi = (int)(4000.0 * s->nfft / rate);
    s->rho = 0.08f;          /* residual echo = -11 dB of the estimate (measured, review-r3) */
    s->over = 2.0f;
    s->gmin_near = 0.7f;     /* -3 dB while the near end talks */
    /* -6 dB at most with only PC audio. MEASURED (round 5, live takes replayed,
     * a quiet external source 2-15 dB under the echo): a -30 dB floor dipped
     * the external by up to 23 dB in 100 ms windows the near-end detector
     * missed; -6 dB caps that at 6 dB and costs the echo reduction 0.2-0.5 dB
     * -- the linear stages do the work. */
    s->gmin_far = 0.5f;
    s->near_ratio = 2.0f;    /* error > 3 dB above the expected residual = near end */
    s->smooth = 0.5f;
    s->release = 0.7f;       /* gain recovery per frame towards a higher target */
    s->hold_frames = 30;     /* 300 ms */
    return s;
}

/* Tunables, all optional. Negative = leave as is. */
API void ds5aec_set(ds5aec *s, float rho, float over, float gmin_near, float gmin_far,
                    float near_ratio, int hold_frames)
{
    if (!s) return;
    if (rho >= 0) s->rho = rho;
    if (over >= 0) s->over = over;
    if (gmin_near >= 0) s->gmin_near = gmin_near;
    if (gmin_far >= 0) s->gmin_far = gmin_far;
    if (near_ratio >= 0) s->near_ratio = near_ratio;
    if (hold_frames >= 0) s->hold_frames = hold_frames;
}

/* A new capture session: the residual stage's adaptive state and its one
 * frame of delay line start from silence (the linear stages are kept; they are
 * reset individually with ds5mdf_reset). */
API void ds5aec_reset(ds5aec *s)
{
    int i;
    if (!s) return;
    memset(s->pe, 0, sizeof(float) * s->nbins);
    memset(s->py, 0, sizeof(float) * s->nbins);
    for (i = 0; i < s->nbins; i++) s->g[i] = 1.0f;
    s->near_hold = 0;
    memset(s->e_prev, 0, sizeof(float) * s->frame);
    memset(s->y_prev, 0, sizeof(float) * s->frame);
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

/* mic: frame int16. stages[i] / refs[i], i < nstage: the linear stages to run
 * this frame, in order, and each one's reference frame (frame * nch int16,
 * interleaved). out: frame int16 (one frame late). gains (optional, nbins
 * floats): the residual gains applied this frame. stats (optional, 8 floats):
 * mic power, linear-output power, echo-estimate power, output power, near-end
 * flag, mean gain, active, reserved. stage_stats (optional, 3 * nstage
 * floats): per stage, the power of its input, of its output and of its own
 * echo estimate (input - output). Returns 0, or -1 on bad arguments (nothing
 * is touched then). */
API int ds5aec_process(ds5aec *s, const int16_t *mic, int nstage, ds5mdf *const *stages,
                       const int16_t *const *refs, int16_t *out, int active,
                       float *gains, float *stats, float *stage_stats)
{
    int i, k, j, n;
    float ecur[4096], ycur[4096], mcur[4096];
    double pmic = 0, plin = 0, pest = 0, pout = 0, gsum = 0;
    int near = 0;
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
    if (active || s->was_active) {
        double se = 0, sr = 0;
        float gmin2;
        const int16_t *cur = mic;
        for (j = 0; j < nstage; j++) {
            ds5mdf *m = stages[j];
            double pe = 0, pin = 0, pou = 0;
            speex_echo_cancellation(m->mdf, cur, refs[j], m->out);
            for (i = 0; i < n; i++) {
                float d = (float)cur[i] - (float)m->out[i];
                pe += (double)d * d;
                pin += (double)cur[i] * cur[i];
                pou += (double)m->out[i] * m->out[i];
            }
            if (stage_stats) {
                stage_stats[3 * j] = (float)(pin / n);
                stage_stats[3 * j + 1] = (float)(pou / n);
                stage_stats[3 * j + 2] = (float)(pe / n);
            }
            cur = m->out;
        }
        for (i = 0; i < n; i++) {
            ecur[i] = (float)cur[i];
            ycur[i] = (float)mic[i] - (float)cur[i];
            plin += (double)ecur[i] * ecur[i];
            pest += (double)ycur[i] * ycur[i];
        }
        stft(s, s->e_prev, ecur, s->E);
        stft(s, s->y_prev, ycur, s->Y);
        stft(s, s->m_prev, mcur, s->X);
        for (k = 0; k < s->nbins; k++) {
            float pe = s->E[k].r * s->E[k].r + s->E[k].i * s->E[k].i;
            float px = s->X[k].r * s->X[k].r + s->X[k].i * s->X[k].i;
            float py;
            /* never worse than no cancellation: where the subtraction ADDED
             * energy (a filter that is wrong right now), keep the mic's bin */
            if (pe > px) { s->E[k] = s->X[k]; pe = px; }
            py = s->Y[k].r * s->Y[k].r + s->Y[k].i * s->Y[k].i;
            s->pe[k] = s->smooth * s->pe[k] + (1.0f - s->smooth) * pe;
            s->py[k] = s->smooth * s->py[k] + (1.0f - s->smooth) * py;
            if (k >= s->band_lo && k <= s->band_hi) {
                se += pe;
                sr += s->over * s->rho * py;
            }
        }
        /* near end: error well above what residual echo explains, in the speech band */
        if (se > s->near_ratio * sr + 1e-3 * (s->band_hi - s->band_lo + 1) * s->nfft) {
            near = 1;
            s->near_hold = s->hold_frames;
        } else if (s->near_hold > 0) {
            s->near_hold--;
            near = 1;
        }
        gmin2 = near ? s->gmin_near * s->gmin_near : s->gmin_far * s->gmin_far;
        for (k = 0; k < s->nbins; k++) {
            float r = s->over * s->rho * s->py[k];
            float g2 = 1.0f - r / (s->pe[k] + 1e-9f);
            float g;
            if (g2 < gmin2) g2 = gmin2;
            if (g2 > 1.0f) g2 = 1.0f;
            g = sqrtf(g2);
            if (g > s->g[k]) g = s->g[k] + s->release * (g - s->g[k]);   /* smooth recovery */
            s->g[k] = g;
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
        memcpy(s->y_prev, ycur, sizeof(float) * n);
    } else if (stage_stats) {
        for (j = 0; j < 3 * nstage; j++) stage_stats[j] = 0.0f;
    }
    memcpy(s->m_prev, mcur, sizeof(float) * n);
    if (!active) {
        /* idle: keep the STFT state as if G == 1 on the raw mic, so a later
         * activation reconstructs seamlessly; the output is the raw delay line */
        for (i = 0; i < n; i++) {
            float w2 = s->win[n + i] * s->win[n + i];
            s->ola[i] = (float)mic[i] * w2;
            s->e_prev[i] = (float)mic[i];
            s->y_prev[i] = 0.0f;
        }
        for (k = 0; k < s->nbins; k++) { s->g[k] = 1.0f; s->pe[k] = 0; s->py[k] = 0; if (gains) gains[k] = 1.0f; }
        gsum = s->nbins;
        s->near_hold = 0;
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
        stats[4] = (float)near; stats[5] = (float)(gsum / s->nbins);
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
