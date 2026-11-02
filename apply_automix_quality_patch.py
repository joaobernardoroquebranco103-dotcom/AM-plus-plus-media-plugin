#!/usr/bin/env python3
"""Apply the AM++ media-plugin Automix quality patch to a local source checkout.

The script is deliberately strict: every expected upstream anchor must occur exactly
once (or an explicitly documented number of times). It stages all edits in memory
and writes files only after every check succeeds.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

AUTO_MIX_MATH = r'''package dev.kifranei.ampp.media

import kotlin.math.cos
import kotlin.math.sin

/** Pure transition math shared by the playback hooks and unit tests. */
internal data class AutoMixGains(
    val outgoing: Float,
    val incoming: Float,
)

internal object AutoMixMath {
    /**
     * Constant-power crossfade: outgoing² + incoming² = 1 throughout the overlap.
     * The selected curve changes timing, not the power law itself.
     */
    fun equalPowerGains(progress: Float, curve: FadeCurve): AutoMixGains {
        val p = if (progress.isFinite()) progress.coerceIn(0f, 1f) else 0f
        val shaped = when (curve) {
            FadeCurve.LINEAR -> p
            FadeCurve.GENTLE -> (0.5f - 0.5f * cos(Math.PI.toFloat() * p))
            FadeCurve.STEEP -> {
                val smooth = p * p * (3f - 2f * p)
                smooth * smooth
            }
        }
        val angle = shaped.toDouble() * Math.PI / 2.0
        return AutoMixGains(
            outgoing = cos(angle).toFloat(),
            incoming = sin(angle).toFloat(),
        )
    }

    fun fallbackDuration(configuredSeconds: Int): Int = configuredSeconds.coerceIn(4, 20)
}
'''

AUTO_MIX_MATH_TEST = r'''package dev.kifranei.ampp.media

import org.junit.Assert.assertEquals
import org.junit.Test

class AutoMixMathTest {
    @Test
    fun `equal power envelope has unity power at all progress points`() {
        for (curve in FadeCurve.values()) {
            for (i in 0..100) {
                val gains = AutoMixMath.equalPowerGains(i / 100f, curve)
                val power = gains.outgoing * gains.outgoing + gains.incoming * gains.incoming
                assertEquals("curve=$curve progress=$i", 1f, power, 0.00001f)
            }
        }
    }

    @Test
    fun `equal power envelope has correct endpoints and midpoint`() {
        val start = AutoMixMath.equalPowerGains(0f, FadeCurve.GENTLE)
        val middle = AutoMixMath.equalPowerGains(0.5f, FadeCurve.GENTLE)
        val end = AutoMixMath.equalPowerGains(1f, FadeCurve.GENTLE)
        assertEquals(1f, start.outgoing, 0.00001f)
        assertEquals(0f, start.incoming, 0.00001f)
        assertEquals(kotlin.math.sqrt(0.5).toFloat(), middle.outgoing, 0.00001f)
        assertEquals(kotlin.math.sqrt(0.5).toFloat(), middle.incoming, 0.00001f)
        assertEquals(0f, end.outgoing, 0.00001f)
        assertEquals(1f, end.incoming, 0.00001f)
    }

    @Test
    fun `non finite progress is safely treated as transition start`() {
        val nan = AutoMixMath.equalPowerGains(Float.NaN, FadeCurve.GENTLE)
        val positiveInfinity = AutoMixMath.equalPowerGains(Float.POSITIVE_INFINITY, FadeCurve.GENTLE)
        assertEquals(1f, nan.outgoing, 0.00001f)
        assertEquals(0f, nan.incoming, 0.00001f)
        assertEquals(1f, positiveInfinity.outgoing, 0.00001f)
        assertEquals(0f, positiveInfinity.incoming, 0.00001f)
    }

    @Test
    fun `fallback duration stays inside safe limits`() {
        assertEquals(4, AutoMixMath.fallbackDuration(2))
        assertEquals(12, AutoMixMath.fallbackDuration(12))
        assertEquals(20, AutoMixMath.fallbackDuration(30))
    }
}
'''

NEW_PLAN = r'''    fun plan(current: SongAudioAnalysis?, next: SongAudioAnalysis?, defaultDurationSec: Int): TransitionPlan {
        // Missing analysis must never block playback or fabricate musical features.
        if (current == null || !current.usable || next == null || !next.usable) {
            return TransitionPlan(
                enabled = true,
                durationSec = AutoMixMath.fallbackDuration(defaultDurationSec),
                curve = FadeCurve.GENTLE,
                reason = "缺少当前或下一首的可靠分析，使用安全淡化；不变速、不跳过前奏",
            )
        }

        val outgoingEnergy = current.energy?.takeIf { it.isFinite() }?.coerceIn(0f, 1f)
        val incomingEnergy = next.energy?.takeIf { it.isFinite() }?.coerceIn(0f, 1f)
        val measuredKeyScore = if (toCamelot(current.key) != null && toCamelot(next.key) != null) {
            camelotCompatibilityScore(current.key, next.key)
        } else null
        val keyScore = measuredKeyScore ?: 0.5f

        val hasBothBpm = current.hasBpm && next.hasBpm &&
            current.bpm!!.isFinite() && next.bpm!!.isFinite() &&
            current.bpm in 60f..200f && next.bpm in 60f..200f
        val tempoRatio = if (hasBothBpm) next.bpm!! / current.bpm!! else null
        val bpmScore = tempoRatio?.takeIf { it.isFinite() && it > 0f }
            ?.let { (1f - abs(kotlin.math.ln(it)) / 0.18f).coerceIn(0f, 1f) }
        val energyScore = if (outgoingEnergy != null && incomingEnergy != null) {
            (1f - abs(outgoingEnergy - incomingEnergy)).coerceIn(0f, 1f)
        } else null
        // Apple-provided danceability is allowed; the local spectral-flatness proxy was removed.
        val danceScore = if (current.danceability?.isFinite() == true && next.danceability?.isFinite() == true) {
            ((current.danceability.coerceIn(0f, 1f) + next.danceability.coerceIn(0f, 1f)) / 2f)
        } else null

        var weightedScore = 0f
        var availableWeight = 0f
        fun includeMetric(score: Float?, weight: Float) {
            if (score != null && score.isFinite()) {
                weightedScore += score.coerceIn(0f, 1f) * weight
                availableWeight += weight
            }
        }
        includeMetric(measuredKeyScore, 0.40f)
        includeMetric(bpmScore, 0.30f)
        includeMetric(energyScore, 0.20f)
        includeMetric(danceScore, 0.10f)
        val mixability = if (availableWeight > 0f) (weightedScore / availableWeight).coerceIn(0f, 1f) else 0.5f

        val mode = when {
            mixability >= 0.72f && (outgoingEnergy ?: 0.5f) > 0.35f -> TransitionMode.HARMONIC_BLEND
            mixability >= 0.32f -> TransitionMode.FILTER_MIX
            else -> TransitionMode.QUICK_CUT
        }

        // The outgoing track is the tempo reference. Only the incoming renderer is adjusted.
        val targetBpm = current.bpm?.takeIf { it.isFinite() && it in 60f..200f } ?: 120f
        val phraseMs = BEATS_PER_PHRASE * (60_000f / targetBpm)
        val baseSec = when (mode) {
            TransitionMode.HARMONIC_BLEND -> 24f + (mixability - 0.72f) * 30f
            TransitionMode.FILTER_MIX -> 10f + (mixability - 0.32f) * 25f
            TransitionMode.QUICK_CUT -> 5f + mixability * 10f
        }
        val phraseCount = (baseSec * 1000f / phraseMs).roundToInt().coerceAtLeast(1)
        val duration = (phraseCount * phraseMs / 1000f).roundToInt().coerceIn(MIN_DURATION, MAX_DURATION)

        val lowEnergy = (outgoingEnergy ?: 0.5f) < 0.30f || (incomingEnergy ?: 0.5f) < 0.30f
        // No verified per-deck speed-control API exists in this host build.
        val curve = when (mode) {
            TransitionMode.HARMONIC_BLEND -> FadeCurve.GENTLE
            TransitionMode.FILTER_MIX -> FadeCurve.STEEP
            TransitionMode.QUICK_CUT -> FadeCurve.LINEAR
        }
        val bpmNote = when {
            !hasBothBpm -> "BPM数据不全"
            lowEnergy -> "低能量淡化"
            else -> "不强制变速"
        }
        val currentBpmLabel = current.bpm?.takeIf { it.isFinite() && it in 60f..200f }?.roundToInt()?.toString() ?: "?"
        val nextBpmLabel = next.bpm?.takeIf { it.isFinite() && it in 60f..200f }?.roundToInt()?.toString() ?: "?"

        return TransitionPlan(
            enabled = true,
            durationSec = duration,
            curve = curve,
            mode = mode,
            mixability = mixability,
            targetBpm = targetBpm,
            reason = "组合 ${mode.name}，mixability=${"%.2f".format(mixability)} " +
                "(key=${"%.2f".format(keyScore)} bpm=${"%.2f".format(bpmScore ?: 0.5f)} " +
                "energy=${"%.2f".format(energyScore ?: 0.5f)})，BPM " +
                "$currentBpmLabel→$nextBpmLabel($bpmNote)，${duration}s/${phraseCount}乐句；" +
                "仅使用已验证的混音参数",
            outroFadeStartMs = current.outroFadeStartMs,
        )
    }

'''


def replace_once(text: str, old: str, new: str, label: str, expected: int = 1) -> str:
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{label}: expected {expected} anchor(s), found {count}")
    return text.replace(old, new)


def replace_all_expected(text: str, old: str, new: str, label: str, minimum: int = 1) -> str:
    count = text.count(old)
    if count < minimum:
        raise RuntimeError(f"{label}: expected at least {minimum} anchor(s), found {count}")
    return text.replace(old, new)


def replace_between(text: str, start: str, end: str, replacement: str, label: str) -> str:
    a = text.find(start)
    if a < 0:
        raise RuntimeError(f"{label}: start marker missing")
    b = text.find(end, a + len(start))
    if b < 0:
        raise RuntimeError(f"{label}: end marker missing")
    return text[:a] + replacement + text[b:]


def build_changes(root: Path) -> dict[Path, str]:
    files = {
        "manifest": root / "media-plugin/plugin.json",
        "entry": root / "media-plugin/src/main/java/dev/kifranei/ampp/media/MediaIntegrationsPlugin.kt",
        "settings_text": root / "media-plugin/src/main/java/dev/kifranei/ampp/media/MediaSettingsText.kt",
        "settings": root / "media-plugin/src/main/java/dev/kifranei/ampp/media/MediaSettings.kt",
        "plugin": root / "media-plugin/src/main/java/dev/kifranei/ampp/media/MediaIntegrationsPlugin.kt",
        "provider": root / "media-plugin/src/main/java/dev/kifranei/ampp/media/AudioAnalysisProvider.kt",
        "extractor": root / "media-plugin/src/main/java/dev/kifranei/ampp/media/AudioFeatureExtractor.kt",
        "planner": root / "media-plugin/src/main/java/dev/kifranei/ampp/media/TransitionPlanner.kt",
        "integration": root / "media-plugin/src/main/java/dev/kifranei/ampp/media/AutoTransitionIntegration.kt",
    }
    missing = [str(p) for p in files.values() if not p.is_file()]
    if missing:
        raise RuntimeError("Run this script from the root of Kifranei/AM-plus-plus-media-plugin; missing: " + ", ".join(missing))
    staged = {key: path.read_text(encoding="utf-8") for key, path in files.items()}

    # Release identity.
    manifest = json.loads(staged["manifest"])
    if manifest.get("versionName") != "1.3.4" or manifest.get("versionCode") != 16:
        raise RuntimeError("Unexpected media-plugin version; expected 1.3.4 / 16 baseline")
    manifest["versionName"] = "1.3.5"
    manifest["versionCode"] = 17
    staged["manifest"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    staged["entry"] = replace_once(staged["entry"], 'internal const val PLUGIN_BUILD = "1.3.4/16"',
                                    'internal const val PLUGIN_BUILD = "1.3.5/17"', "plugin build label")

    staged["entry"] = replace_once(
        staged["entry"],
        'this.text = "过渡时长和起点由歌曲分析自动决定，不再固定为某个秒数。"',
        'this.text = "按双方有效分析规划接歌；不强制改变播放速度或猜测跳过前奏。"',
        "settings safety note",
    )
    staged["settings_text"] = replace_once(
        staged["settings_text"],
        'val autoTransitionDescription get() = text("按歌曲调性/BPM/小节/能量自动选择接歌点与过渡方式；变速只作用于 incoming renderer。",\n'
        '        "Intelligent crossfade with tempo/key matching and beat-matched speed adjustment. Experimental; requires Apple Music 7.0.0-beta.")',
        'val autoTransitionDescription get() = text("按双方可靠分析规划淡化；使用出场歌曲 BPM 作为节奏参考，不强制改变播放速度。",\n'
        '        "Plans fades from reliable track analysis, using the outgoing track BPM as the timing reference without changing playback speed.")',
        "Automix settings description",
    )
    staged["settings_text"] = replace_once(staged["settings_text"],
        '    val autoTransitionSpeedMatch get() = text("变速匹配（Beat Match）", "Speed match (Beat Match)")\n',
        '', "remove unverified beat-match label")
    staged["settings"] = replace_once(staged["settings"],
        '    // 变速匹配（Beat Match）默认关闭：setPlaybackParameters 是对整个 ExoPlayer 生效的，\n'
        '    // crossfade 期间当前歌也被变速，切换瞬间 AudioTrack 重建会导致"音乐被切断+纯杂音"（用户实测）。\n'
        '    val autoTransitionSpeedMatch: Boolean = false,\n',
        '', "remove unverified beat-match setting")
    staged["settings"] = replace_once(staged["settings"],
        '        put("auto_transition_speed_match", autoTransitionSpeedMatch)\n',
        '', "stop persisting unused beat-match setting")
    staged["settings"] = replace_once(staged["settings"],
        '                json.opt("auto_transition_speed_match") as? Boolean ?: false,\n',
        '', "ignore legacy beat-match setting")
    staged["plugin"] = replace_once(staged["plugin"],
        '                speedMatch = { store.read().autoTransitionSpeedMatch },\n',
        '', "remove unused speed-match constructor argument")
    staged["plugin"] = replace_once(staged["plugin"],
        '        option(text.autoTransitionSpeedMatch, draft.autoTransitionSpeedMatch) { state, enabled -> state.copy(autoTransitionSpeedMatch = enabled) }\n',
        '', "remove inert speed-match switch")
    staged["integration"] = replace_once(staged["integration"],
        '    private val speedMatch: () -> Boolean,\n',
        '', "remove unused speed-match callback")

    # Analysis cache: use bounded strong keys and prefer Apple analysis for both decks.
    staged["provider"] = replace_once(staged["provider"], "import java.util.WeakHashMap", "import java.util.LinkedHashMap", "analysis cache import")
    cache_class = '''\n/** Small, synchronized LRU: String keys must not disappear through weak-key collection. */\nprivate class BoundedAnalysisCache<K, V>(private val capacity: Int) {\n    private val values = object : LinkedHashMap<K, V>(capacity, 0.75f, true) {\n        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<K, V>?): Boolean = size > capacity\n    }\n\n    @Synchronized operator fun get(key: K): V? = values[key]\n    @Synchronized operator fun set(key: K, value: V) { values[key] = value }\n}\n'''
    staged["provider"] = replace_once(staged["provider"], "import java.util.LinkedHashMap\n", "import java.util.LinkedHashMap\n" + cache_class, "bounded cache helper")
    staged["provider"] = replace_between(staged["provider"],
        '/** 简化后的歌曲音频分析数据，供过渡规划使用。 */\ninternal data class SongAudioAnalysis(\n',
        '\n) {',
        '/** Only retain analysis fields consumed by the live transition planner. */\n'
        'internal data class SongAudioAnalysis(\n'
        '    val bpm: Float?,\n'
        '    val key: String?,\n'
        '    val energy: Float?,\n'
        '    val danceability: Float?,\n'
        '    val outroFadeStartMs: Float? = null,\n'
        '    val loudnessCurve: FloatArray? = null,\n'
        '    val barTimesSec: FloatArray? = null,\n'
        ')',
        "remove analysis properties with no remaining runtime consumers")
    staged["provider"] = replace_once(staged["provider"],
        '            acousticness = callObject(analysis, "getAcousticness")?.let { getDoubleField(it, "main") }?.toFloat(),\n',
        '', "remove unused acousticness lookup")
    staged["provider"] = replace_once(staged["provider"],
        '            outroFadeEndMs = readFadeMs(analysis, "getFadeOut", "getEndInMilliseconds"),\n'
        '            introFadeStartMs = readFadeMs(analysis, "getFadeIn", "getStartInMilliseconds"),\n'
        '            introFadeEndMs = readFadeMs(analysis, "getFadeIn", "getEndInMilliseconds"),\n',
        '', "remove unused fade-in and fade-out-end cues")
    staged["provider"] = replace_once(staged["provider"],
        '            beatTimesSec = readBeatArray(analysis, "getBeatsInMilliseconds"),\n',
        '', "remove unused Apple beat-array lookup")
    staged["provider"] = replace_once(staged["provider"],
        'private val cache = WeakHashMap<String, SongAudioAnalysis>()',
        'private val cache = BoundedAnalysisCache<String, SongAudioAnalysis>(128)', "Apple analysis cache")
    staged["provider"] = replace_once(staged["provider"],
        'private val localCache = WeakHashMap<String, SongAudioAnalysis>()',
        'private val localCache = BoundedAnalysisCache<String, SongAudioAnalysis>(128)', "local analysis cache")
    staged["provider"] = replace_once(staged["provider"],
        'current = currentId?.let { localCache[it] ?: cache[it] }',
        'current = currentId?.let { cache[it] ?: localCache[it] }', "prefer Apple analysis for outgoing")
    old_fetch = '''        val method = fadeControl.javaClass.getDeclaredMethod("getAudioAnalysis", playerMediaItemClass!!)
        method.isAccessible = true
        val analysis = method.invoke(fadeControl, mediaItem)
        if (analysis == null) {
            FileLogger.log("AudioAnalysis: getAudioAnalysis returned null for item=${mediaItem.javaClass.name}")
            // LibraryMediaItem 不走宿主分析；尝试 Apple 内部按 store id 取分析缓存（n9.d.e(String)）。
            val id = extractItemId(mediaItem)
            val byId = id?.let { fetchAppleAnalysisById(it) }
            if (byId != null) return byId
            return null
        }
        val parsed = parse(analysis) ?: return null
        // 写回缓存，供下次过渡直接使用
        val id = extractItemId(mediaItem)
        if (id != null) cache[id] = parsed
        parsed'''
    new_fetch = '''        // LibraryMediaItem can fail the PlayerMediaItem reflective signature. Still try
        // the host's id-based analysis cache instead of returning on that type mismatch.
        val id = extractItemId(mediaItem)
        val analysis = runCatching {
            fadeControl.javaClass.getDeclaredMethod("getAudioAnalysis", playerMediaItemClass!!).apply {
                isAccessible = true
            }.invoke(fadeControl, mediaItem)
        }.getOrNull()
        val parsed = analysis?.let(::parse)
        if (parsed == null) {
            FileLogger.log("AudioAnalysis: direct analysis unavailable for item=${mediaItem.javaClass.name}; trying id cache")
            return id?.let(::fetchAppleAnalysisById)
        }
        if (id != null) cache[id] = parsed
        parsed'''
    staged["provider"] = replace_once(staged["provider"], old_fetch, new_fetch, "analysis invocation fallback")
    old_id = '''    private fun extractItemId(mediaItem: Any): String? = runCatching {
        mediaItem.javaClass.getMethod("getId").invoke(mediaItem) as? String
            ?: mediaItem.javaClass.getMethod("getStoreId").invoke(mediaItem) as? String
    }.getOrNull()'''
    new_id = '''    private fun extractItemId(mediaItem: Any): String? = listOf("getId", "getStoreId")
        .firstNotNullOfOrNull { name ->
            runCatching { mediaItem.javaClass.getMethod(name).invoke(mediaItem) as? String }
                .getOrNull()?.takeIf { it.isNotBlank() }
        }'''
    staged["provider"] = replace_once(staged["provider"], old_id, new_id, "item ID fallback")

    # Local PCM fallback must not call spectral flatness "danceability" or waste time on it.
    staged["extractor"] = replace_once(staged["extractor"],
        ' *  - 舞曲性：频谱平坦度（spectral flatness），0-1，越高越像舞曲/噪声',
        ' *  - 不再把频谱平坦度冒充舞曲性；缺少可靠指标时由规划器忽略该项', "feature extractor docs")
    staged["extractor"] = replace_once(staged["extractor"],
        'return SongAudioAnalysis(null, null, rms, 0.5f, null)',
        'return SongAudioAnalysis(null, null, rms, null)',
        "short-window analysis must not fabricate danceability")
    staged["extractor"] = replace_once(staged["extractor"],
        '        var flatnessSum = 0f\n        var flatnessCount = 0\n', '', "flatness accumulators")
    staged["extractor"] = replace_once(staged["extractor"],
        '''
            // 频谱平坦度
            val flat = spectralFlatness(mag)
            if (flat > 0f) {
                flatnessSum += flat
                flatnessCount++
            }''', '', "flatness frame work")
    staged["extractor"] = replace_once(staged["extractor"],
        '''
        // 舞曲性：频谱平坦度平均
        val danceability = if (flatnessCount > 0) flatnessSum / flatnessCount else 0.5f''', '', "fake danceability calculation")
    staged["extractor"] = replace_once(staged["extractor"],
        '            danceability = danceability,', '            danceability = null,', "remove fake danceability")
    flatness_fn = '''    private fun spectralFlatness(mag: FloatArray): Float {
        var logSum = 0.0
        var linSum = 0f
        var count = 0
        val eps = 1e-10f
        for (m in mag) {
            if (m > eps) {
                logSum += ln(m.toDouble())
                linSum += m
                count++
            }
        }
        if (count == 0 || linSum <= 0f) return 0f
        val geoMean = exp(logSum / count).toFloat()
        val arithMean = linSum / count
        return (geoMean / arithMean).coerceIn(0f, 1f)
    }

'''
    staged["extractor"] = replace_once(staged["extractor"], flatness_fn, '', "remove unused spectral flatness function")
    staged["extractor"] = replace_once(staged["extractor"],
        'return SongAudioAnalysis(null, null, 0.5f, 0.5f, null)',
        'return SongAudioAnalysis(null, null, 0.5f, 0.5f)',
        "align empty PCM fallback with lean analysis model")
    staged["extractor"] = replace_once(staged["extractor"],
        '        // 节拍网格：从能量通量提取 onset 峰值，转换为时间点（秒）\n'
        '        val beats = detectBeats(energies, sampleRate)\n',
        '', "remove unused PCM beat grid")
    staged["extractor"] = replace_once(staged["extractor"],
        '        val frameDuration = HOP_SIZE.toFloat() / sampleRate\n'
        '        val silenceThreshold = computeSilenceThreshold(energies)\n'
        '        val (startSilenceFrames, endSilenceFrames) = detectSilence(energies, silenceThreshold)\n'
        '        val startSilenceSec = startSilenceFrames * frameDuration\n'
        '        val endSilenceSec = endSilenceFrames * frameDuration\n\n'
        '        // Intro 检测：能量首次显著上升的点（从开头静音/低能量到主歌/副歌的过渡）\n'
        '        val introEndSec = detectIntroEnd(energies, frameDuration, startSilenceFrames)\n\n',
        '', "remove unused silence and intro analysis")
    staged["extractor"] = replace_once(staged["extractor"],
        '            acousticness = null,\n'
        '            beats = beats,\n'
        '            startSilenceSec = startSilenceSec,\n'
        '            endSilenceSec = endSilenceSec,\n'
        '            introEndSec = introEndSec,\n',
        '', "remove unused PCM analysis result fields")
    staged["extractor"] = replace_between(staged["extractor"],
        '    /** 计算静音阈值：帧能量中位数的 1/100（约 -40dB），最低兜底。 */\n',
        '    // ---- BPM 检测 ----',
        '', "remove unused silence and intro analysis helpers")
    staged["extractor"] = replace_between(staged["extractor"],
        '    // ---- 节拍网格检测 ----\n',
        '    // ---- 调性检测 ----',
        '', "remove unused PCM beat-grid detector")

    # Replace the planner as one transaction. The following Camelot scoring helpers remain intact.
    plan_start = '    fun plan(current: SongAudioAnalysis?, next: SongAudioAnalysis?, defaultDurationSec: Int): TransitionPlan {\n'
    plan_end = '    /**\n     * Camelot 轮调性兼容性连续评分（0~1）。'
    staged["planner"] = replace_between(staged["planner"], plan_start, plan_end, NEW_PLAN, "TransitionPlanner.plan")
    staged["planner"] = replace_once(staged["planner"], '    private var lastEntryLog = ""\n', '', "remove unused plan-entry logging state")
    staged["planner"] = replace_once(staged["planner"],
        'internal enum class TransitionMode { HARMONIC_BLEND, BASS_SWAP, FILTER_MIX, QUICK_CUT }',
        'internal enum class TransitionMode { HARMONIC_BLEND, FILTER_MIX, QUICK_CUT }',
        "remove unavailable bass-swap mode")
    staged["planner"] = replace_between(staged["planner"],
        'internal data class TransitionPlan(\n',
        '\n)\n',
        'internal data class TransitionPlan(\n'
        '    val enabled: Boolean,\n'
        '    val durationSec: Int,\n'
        '    val curve: FadeCurve = FadeCurve.LINEAR,\n'
        '    val reason: String = "",\n'
        '    val mode: TransitionMode = TransitionMode.FILTER_MIX,\n'
        '    val mixability: Float = 0.5f,\n'
        '    val targetBpm: Float? = null,\n'
        '    /** Apple-authored outgoing fade-out cue in milliseconds. */\n'
        '    val outroFadeStartMs: Float? = null,\n'
        ')',
        "remove unused plan fields with no runtime consumers")
    staged["planner"] = replace_between(staged["planner"],
        '    private fun findNearestBeat(beats: FloatArray?, targetSec: Int): Float? {\n',
        '    /** 从 beats 或 BPM 计算乐句时长（毫秒）。优先用实际节拍间隔的中位数。 */',
        '', "remove obsolete nearest-beat helper")
    staged["planner"] = replace_between(staged["planner"],
        '    private fun phraseMsFrom(song: SongAudioAnalysis): Float {\n',
        '    /** Camelot 调性兼容性（离散等级，保留用于向后兼容）。 */',
        '', "remove unused local beat-grid phrase estimator")
    staged["planner"] = replace_between(staged["planner"],
        '    private fun entryTarget(mode: TransitionMode): Float = when (mode) {\n',
        '    fun camelotCompatibilityScore(keyA: String?, keyB: String?): Float {',
        '', "remove unused bar-entry helpers")
    staged["planner"] = replace_once(staged["planner"],
        '    private fun keyCompatibilityLabel(c: KeyCompatibility) = when (c) {\n'
        '        KeyCompatibility.PERFECT -> "完美兼容"\n'
        '        KeyCompatibility.GOOD -> "良好兼容"\n'
        '        KeyCompatibility.FAIR -> "一般"\n'
        '        KeyCompatibility.POOR -> "不兼容"\n'
        '    }\n',
        '', "remove unused localized key compatibility label")
    staged["planner"] = replace_between(staged["planner"],
        '    /** 从连续评分映射到离散兼容等级（用于日志/UI）。 */\n',
        '    /**\n     * 动态过渡时长决策：',
        '', "remove unused discrete key compatibility and duration helpers")
    staged["planner"] = replace_between(staged["planner"],
        '    /** Camelot 调性兼容性（离散等级，保留用于向后兼容）。 */\n',
        '    private fun toCamelot(key: String?): Pair<Int, Char>? {',
        '', "remove unused public discrete key-compatibility wrapper")
    staged["planner"] = replace_once(staged["planner"],
        'internal enum class KeyCompatibility { PERFECT, GOOD, FAIR, POOR }\n',
        '', "remove unused key compatibility enum")

    # Playback integration fixes. Each anchor is exact and checked before any file is written.
    staged["integration"] = replace_once(staged["integration"],
        '            AutoTransitionUi.onToggle = { newState ->\n'
        '                runtimeEnabled = newState\n'
        '                applyCrossfadeState(newState)\n'
        '                flog("UI toggled -> $newState")\n'
        '            }',
        '            AutoTransitionUi.onToggle = { newState ->\n'
        '                runtimeEnabled = newState\n'
        '                applyCrossfadeState(newState)\n'
        '                if (!newState) {\n'
        '                    if (crossfadeInProgress) {\n'
        '                        endActualTransitionLog()\n'
        '                        finalizeCoverTransition()\n'
        '                    } else {\n'
                '                        AutoTransitionProgressBar.setTransitionActive(false)\n'
        '                    }\n'
        '                }\n'
        '                flog("UI toggled -> $newState")\n'
        '            }',
        "restore audio state when user disables AutoMix")
    staged["integration"] = replace_once(staged["integration"],
        '        val scope = PluginScope()\n        try {',
        '        val scope = PluginScope()\n'
        '        scope.onClose {\n'
        '            main.post {\n'
        '                runtimeEnabled = false\n'
                '                if (crossfadeInProgress) {\n'
        '                    endActualTransitionLog()\n'
        '                    finalizeCoverTransition()\n'
        '                } else {\n'
        '                    AutoTransitionProgressBar.setTransitionActive(false)\n'
        '                }\n'
        '            }\n'
        '        }\n'
        '        try {',
        "restore playback state on plugin-scope close")
    staged["integration"] = replace_once(staged["integration"],
        '            val analyzeExecutor = java.util.concurrent.Executors.newSingleThreadExecutor()\n',
        '            val analyzeExecutor = java.util.concurrent.Executors.newSingleThreadExecutor()\n'
        '            scope.onClose { analyzeExecutor.shutdownNow() }\n',
        "stop PCM analysis worker on capability close")
    staged["integration"] = replace_once(staged["integration"],
        '            // ---- 1) 音频分析数据提供者（hook getAudioAnalysis + 主动获取）----\n            analysis.install(scope)\n',
        '', "remove duplicate analysis-hook installation")
    staged["integration"] = replace_all_expected(staged["integration"],
        'TransitionPlanner.plan(analysis.current, analysis.next, 8)',
        'TransitionPlanner.plan(analysis.current, analysis.next, defaultDurationSec().coerceIn(4, 20))',
        "honor configured fallback duration", minimum=2)
    staged["integration"] = replace_once(staged["integration"],
        '                        crossfadeInProgress = true\n                        crossfadeStartTimeMs = android.os.SystemClock.elapsedRealtime()\n',
        '                        crossfadeInProgress = false\n                        crossfadeStartTimeMs = 0L\n',
        "do not start transition clock in prepare")
    staged["integration"] = replace_once(staged["integration"],
        '                        AutoTransitionProgressBar.setBpm(analysis.current?.bpm)\n'
        '                        AutoTransitionProgressBar.setTransitionActive(true)\n',
        '                        AutoTransitionProgressBar.setBpm(analysis.current?.bpm)\n',
        "defer transition progress UI until actual overlap")
    staged["integration"] = replace_once(staged["integration"],
        '                                    runCatching { AutoTransitionProgressBar.setTransitionActive(true) }\n',
        '                                    // Progress UI is activated by the confirmed audio-overlap callback.\n',
        "do not activate progress UI from artwork timing")
    staged["integration"] = replace_once(staged["integration"],
        '                        transitionEndRunnable?.let { coverHoldHandler.removeCallbacks(it) }\n'
        '                        val endRunnable = Runnable { finalizeCoverTransition() }\n'
        '                        transitionEndRunnable = endRunnable\n'
        '                        coverHoldHandler.postDelayed(endRunnable, (plan.durationSec * 1000L) + 8000L)\n'
        '                        flog("transitionEndTimeout 已注册: ${plan.durationSec + 8}s")\n',
        '                        // Timeout is armed only after maybeStartCrossFading confirms an overlap.\n',
        "do not arm end timeout during prepare")
    eq_start = '''                                AutoMixEqualizer.start(
                                    (planNow?.durationSec ?: 8) * 1000L,
                                    planNow?.eqDepthDb ?: 6f,
                                    planNow?.mode?.name ?: "FILTER_MIX",
                                )
'''
    staged["integration"] = replace_once(staged["integration"], eq_start, '',
        "remove global EQ; no unverified per-deck DSP or tempo adjustment")
    staged["integration"] = replace_once(staged["integration"],
        '                        // 首静音/Intro 跳过：如果下一首开头有较长静音或 intro，\n'
        '                        // 反射设置 fadeInPeriodHolder 的起始位置，跳过空白段。\n'
        '                        // 取首静音和 intro 结束位置的较大值（至少 1 秒才跳过）。\n'
        '                        val skipSec = activePlan?.introSkipSec ?: 4f\n'
        '                        if (skipSec > 1f && !introSkipApplied) {\n'
        '                            introSkipApplied = true\n'
        '                            skipNextSongIntro(param.args[1], skipSec)\n'
        '                            flog("skip intro(prepare): ${"%.1f".format(skipSec)}s (plan ${activePlan?.mode?.name})")\n'
        '                        }\n',
        '', "remove speculative intro seek in prepare")
    staged["integration"] = replace_once(staged["integration"],
        '                                val skip = planNow?.introSkipSec ?: 0f\n'
        '                                if (skip > 1f && !introSkipApplied) {\n'
        '                                    introSkipApplied = true\n'
        '                                    skipNextSongIntro(param.args.getOrNull(1), skip)\n'
        '                                    flog("skip intro(maybeStart): ${"%.1f".format(skip)}s (plan ${planNow?.mode?.name})")\n'
        '                                }\n',
        '', "remove speculative intro seek after host start")
    staged["integration"] = replace_once(staged["integration"],
        '    @Volatile private var introSkipApplied = false\n', '', "remove unused intro-skip state")
    staged["integration"] = replace_all_expected(staged["integration"],
        '        introSkipApplied = false\n', '', "remove unused intro-skip resets", minimum=2)
    staged["integration"] = replace_between(staged["integration"],
        '    private fun skipNextSongIntro(fadeInHolder: Any?, skipSec: Float) {\n',
        '\n}\n\n// ============================================================\n// 噪声溶解 Drawable',
        '', "remove unused speculative seek implementation")
    staged["integration"] = replace_once(staged["integration"],
        '                        BassSwapProcessor.reset()  // 重置低频交换滤波器状态\n',
        '', "remove unused mixed-PCM bass processor call")
    staged["integration"] = replace_once(staged["integration"],
        '            AutoMixEqualizer.stop()\n', '', "remove inert global EQ cleanup")
    staged["integration"] = replace_once(staged["integration"],
        '        val eq = AutoMixEqualizer.snapshot()\n', '', "remove inert global EQ snapshot")
    staged["integration"] = replace_once(staged["integration"],
        '                "curve=${plan?.curve} eqDepth=${plan?.eqDepthDb}dB inLevel=${fadeFloat("fadeInLevel")} outLevel=${fadeFloat("fadeOutLevel")} eq=[$eq]"\n',
        '                "curve=${plan?.curve} inLevel=${fadeFloat("fadeInLevel")} outLevel=${fadeFloat("fadeOutLevel")}"\n',
        "remove false global EQ telemetry from transition start")
    staged["integration"] = replace_once(staged["integration"],
        '                        "eq=[${AutoMixEqualizer.snapshot()}]"\n',
        '', "remove inert global EQ tick telemetry")
    staged["integration"] = replace_once(staged["integration"],
        '                "incomingPos=${(actualPositionMs() ?: lastPositionMs) / 1000f}s inActualStart=${transitionInStartUs?.div(1_000_000f)}s eq=[${AutoMixEqualizer.snapshot()}]"\n',
        '                "incomingPos=${(actualPositionMs() ?: lastPositionMs) / 1000f}s inActualStart=${transitionInStartUs?.div(1_000_000f)}s"\n',
        "remove inert global EQ end telemetry")
    staged["integration"] = replace_between(staged["integration"],
        '    private fun curveValue(progress: Float, curve: FadeCurve): Float = when (curve) {\n',
        '    private fun setFloatField(target: Any?, name: String, value: Float) {\n',
        '', "remove superseded curve helper")
    staged["integration"] = replace_once(staged["integration"],
        '                        val equalPower = Math.sin(progress * Math.PI / 2.0).let { it * it }.toFloat()\n'
        '                        param.result = equalPower',
        '                        param.result = AutoMixMath.equalPowerGains(\n'
        '                            progress,\n'
        '                            activePlan?.curve ?: FadeCurve.GENTLE,\n'
        '                        ).incoming',
        "use equal-power fade-in")
    staged["integration"] = replace_once(staged["integration"],
        '                val inLevel = curveValue(progress, transitionCurve)\n'
        '                setFloatField(transitionFadeControl, "fadeInLevel", inLevel)\n'
        '                setFloatField(transitionFadeControl, "fadeOutLevel", outLevel)',
        '                val gains = AutoMixMath.equalPowerGains(progress, transitionCurve)\n'
        '                setFloatField(transitionFadeControl, "fadeInLevel", gains.incoming)\n'
        '                setFloatField(transitionFadeControl, "fadeOutLevel", gains.outgoing)',
        "use paired equal-power gains")
    staged["integration"] = replace_once(staged["integration"],
        '                        val result = param.result as? Boolean\n',
        '                        val result = param.result as? Boolean\n',
        "restore renderer on every non-start tick, not only a boolean edge")
    staged["integration"] = replace_once(staged["integration"],
        '        transitionStartedMs = android.os.SystemClock.elapsedRealtime()\n',
        '        transitionStartedMs = android.os.SystemClock.elapsedRealtime()\n'
        '        crossfadeStartTimeMs = transitionStartedMs\n'
        '        crossfadeInProgress = true\n'
        '        AutoTransitionProgressBar.setTransitionActive(true)\n',
        "start playback clock only when overlap really starts")
    staged["integration"] = replace_once(staged["integration"],
        '        transitionDurationMs = ((plan?.durationSec ?: 0) * 1000L).coerceAtLeast(4000L)\n',
        '        transitionDurationMs = ((plan?.durationSec ?: 0) * 1000L).coerceAtLeast(4000L)\n'
        '        transitionEndRunnable?.let { coverHoldHandler.removeCallbacks(it) }\n'
        '        val endRunnable = Runnable {\n'
        '            if (crossfadeInProgress) {\n'
        '                endActualTransitionLog()\n'
        '                finalizeCoverTransition()\n'
        '                flog("transition timeout; playback state restored")\n'
        '            }\n'
        '        }\n'
        '        transitionEndRunnable = endRunnable\n'
        '        coverHoldHandler.postDelayed(endRunnable, transitionDurationMs + 8000L)\n',
        "arm transition timeout using the current plan duration")
    staged["integration"] = replace_once(staged["integration"],
        '                        val curSilence = analysis.current?.let { "尾静${"%.1f".format(it.endSilenceSec)}s" } ?: ""\n'
        '                        val nxtSilence = analysis.next?.let { "首静${"%.1f".format(it.startSilenceSec)}s intro${"%.1f".format(it.introEndSec)}s" } ?: ""\n'
        '                        flog("plan=${plan.reason} duration=${plan.durationSec}s cur=${analysis.current?.bpm} nxt=${analysis.next?.bpm} $curSilence $nxtSilence")\n',
        '                        flog("plan=${plan.reason} duration=${plan.durationSec}s cur=${analysis.current?.bpm} nxt=${analysis.next?.bpm}")\n',
        "remove diagnostics for removed intro/silence estimates")
    staged["integration"] = replace_once(staged["integration"],
        '                        // 只对 fadeIn 单个 renderer 设置速度，避免全局 PlaybackParameters 导致双渲染 AudioTrack glitch。\n'
        '                        if (plan.needsSpeedMatch && speedMatch()) applyRendererSpeed(param.thisObject, param.args.getOrNull(1), plan.speedRatio)\n',
        '', "remove unverified per-renderer speed adjustment")
    staged["integration"] = replace_between(staged["integration"],
        '    private fun applyRendererSpeed(fadeControl: Any?, inHolder: Any?, ratio: Float) {\n',
        '    private fun actualPositionMs(): Long? = runCatching {\n',
        '', "remove unverified renderer speed implementation")
    staged["integration"] = replace_between(staged["integration"],
        '    /** 淡化结束后恢复原速。 */\n    private fun restoreSpeed() {\n',
        '    /** 直接调用 ExoMediaPlayer.setCrossFadeState(int)',
        '', "remove unsafe whole-player speed reset")
    staged["integration"] = replace_once(staged["integration"],
        '    private var speedRenderer: Any? = null\n    private var playbackParamsClass: Class<*>? = null\n',
        '', "remove unused speed renderer state")
    staged["integration"] = replace_once(staged["integration"],
        '            if (speedMatch()) restoreSpeed()\n',
        '', "remove obsolete speed reset call")
    staged["integration"] = replace_once(staged["integration"],
        '            applyCrossfadeDuration(8)\n',
        '            applyCrossfadeDuration(defaultDurationSec().coerceIn(4, 20))\n',
        "honor configured native crossfade duration")
    staged["integration"] = replace_once(staged["integration"],
        '            TransitionMode.BASS_SWAP -> 24f to 7f\n',
        '', "remove unsupported per-deck bass-swap cue mode")
    staged["integration"] = replace_once(staged["integration"],
        '        val bars = current.barTimesSec ?: current.beatTimesSec ?: return plan\n        if (bars.size < 8) return plan',
        '''        val appleOutroStartSec = current.outroFadeStartMs?.div(1000f)
        if (appleOutroStartSec != null && appleOutroStartSec.isFinite()) {
            val overlap = durationSec - appleOutroStartSec
            if (appleOutroStartSec > durationSec * 0.35f && overlap in 4f..45f) {
                val seconds = overlap.toInt().coerceIn(4, 40)
                return plan.copy(
                    durationSec = seconds,
                    reason = plan.reason + "; Apple outro cue at ${"%.1f".format(appleOutroStartSec)}s",
                )
            }
        }
        // A beat grid is not a bar grid. Only explicit bar boundaries are considered.
        val bars = current.barTimesSec ?: return plan
        if (bars.size < 2) return plan''',
        "prefer Apple-authored outro cue and do not treat beats as bars")

    # A visible capability must have all essential audio hooks. UI-only partial installs are not Automix.
    staged["integration"] = replace_once(staged["integration"],
        '            var applied = 0\n',
        '            var applied = 0\n            var baseContextDurationInstalled = false\n',
        "track BaseMediaPlayerContext hook")
    staged["integration"] = replace_once(staged["integration"],
        '                }, scope).let { if (it) applied++ }\n'
        '            }.onFailure { flog("hook BaseMediaPlayerContext.getCrossFadeDuration failed: ${it}") }',
        '                }, scope).let { if (it) { applied++; baseContextDurationInstalled = true } }\n'
        '            }.onFailure { flog("hook BaseMediaPlayerContext.getCrossFadeDuration failed: ${it}") }',
        "record essential duration hook")
    staged["integration"] = replace_once(staged["integration"],
        '            scope.activate()\n',
        '            if (setCrossFadeStateMethod == null || applePrepare == null || maybeStart == null || isEnabled == null ||\n'
        '                isTransData == null || calcFadeIn == null || !baseContextDurationInstalled\n'
        '            ) {\n'
        '                scope.close()\n'
        '                return TargetCapabilityInstall.Unsupported(\n'
        '                    "AutoMix audio hooks incomplete; this host build is not verified for the transition engine",\n'
        '                )\n'
        '            }\n'
        '            scope.activate()\n',
        "refuse partial audio-hook capability")

    # Write the new shared math and test only after all old-source anchors passed.
    math_path = root / "media-plugin/src/main/java/dev/kifranei/ampp/media/AutoMixMath.kt"
    test_path = root / "media-plugin/src/test/java/dev/kifranei/ampp/media/AutoMixMathTest.kt"
    if math_path.exists() or test_path.exists():
        raise RuntimeError("AutoMixMath files already exist; refusing to overwrite")
    staged["__math__"] = AUTO_MIX_MATH
    staged["__test__"] = AUTO_MIX_MATH_TEST
    output_paths = {files_key: files[files_key] for files_key in files}
    output_paths["__math__"] = math_path
    output_paths["__test__"] = test_path

    # Store modified content in a side channel for the caller.
    return {path: staged[key] for key, path in output_paths.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes after all anchors validate; default is dry-run")
    args = parser.parse_args()
    root = Path.cwd()
    try:
        changes = build_changes(root)
    except Exception as exc:
        print(f"PATCH ABORTED: {exc}", file=sys.stderr)
        return 2

    delete_paths = [
        root / "media-plugin/src/main/java/dev/kifranei/ampp/media/AutoMixEqualizer.kt",
        root / "media-plugin/src/main/java/dev/kifranei/ampp/media/BassSwapProcessor.kt",
    ]
    missing_deletes = [path for path in delete_paths if not path.is_file()]
    if missing_deletes:
        print("PATCH ABORTED: expected obsolete source files missing: " + ", ".join(str(p) for p in missing_deletes), file=sys.stderr)
        return 2

    print(f"Validated {len(changes)} modified files, {len(delete_paths)} removals, and all source anchors.")
    for path, content in changes.items():
        print(f"  {path.relative_to(root)}: {len(content.encode('utf-8'))} bytes staged")
    for path in delete_paths:
        print(f"  DELETE {path.relative_to(root)}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to write the staged source changes.")
        return 0

    # All contents and removals are staged/validated before the first write.
    for path, content in changes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    for path in delete_paths:
        path.unlink()
    print("Applied Automix source patch. Next: run the media-plugin unit tests and build the ZIP.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
