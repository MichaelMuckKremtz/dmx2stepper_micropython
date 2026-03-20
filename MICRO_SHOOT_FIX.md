# Micro-Shoot Fix: Next Priority

## Issue Description

During linear fades, the motor exhibits **periodic micro-shoots** - sudden drops of 5-14px followed by quick recovery (40-80ms). These create a jagged, non-smooth appearance in the motion path.

### Identified Shoot Timestamps (from 2-min capture)
| Time (s) | Drop (px) | Recovery (ms) | X Position |
|----------|-----------|--------------|------------|
| 4.75 | 5px | 40ms | 371→366→377 |
| 6.5 | 12px | 40ms | 449→437→448 |
| 26.5 | 14px | 200ms | 230→216→227 |
| 30.5 | 7px | 40ms | 301→294→304 |
| 40.5 | 10px | 80ms | 512→502→505 |
| 49.5 | 5px | 40ms | 251→246→257 |
| 53 | 13px | 40ms | 446→433→445 |
| 58 | 9px | 240ms | 563→554→562 |

**Pattern**: Shoot every 3-6 seconds, 5-14px drop, recovers within 1-2 frames.

## Root Cause Hypothesis

The micro-shoots appear to be **controller hunting** caused by:

1. **Chunk-based motion**: 64-step chunks create discrete motion bursts
2. **Position overshoot**: Motor overshoots target, then controller corrects
3. **Tracking deadband interaction**: `POSITION_TRACKING_DEADBAND=5` triggers corrections

The pattern repeats every 3-6 seconds suggesting a systematic issue in the position feedback loop.

## Current State

### Configuration (config.py)
```python
LINEAR_FADE_WINDOW = 16
LINEAR_FADE_VARIANCE_THRESH = 5.0
LINEAR_FADE_CHUNK_STEPS = 4
LINEAR_FADE_MIN_STEPS_SEC = 30
LINEAR_FADE_BLEND_MS = 200
VELOCITY_DEADBAND_HZ = 500
POSITION_TRACKING_DEADBAND = 5
```

### Achieved Metrics
- Good fades (stdev < 5): ~73%
- Average stdev: 1.81px
- Best stdev: 1.05px

## End Goal

**Target**: Eliminate periodic micro-shoots during linear fades.

### Success Criteria
1. No drops > 5px during fades
2. Position stays within ±3px of ideal linear path
3. Smooth motion with no visible hunting pattern
4. Consistent 60+ fps smoothness

## Proposed Fixes

### Option 1: Reduce Step Chunk Size
- Currently: `LINEAR_FADE_CHUNK_STEPS = 4`
- Try: `LINEAR_FADE_CHUNK_STEPS = 2` or `1`
- Rationale: Smaller chunks = finer position control = less overshoot

### Option 2: Increase Tracking Deadband
- Currently: `POSITION_TRACKING_DEADBAND = 5`
- Try: `POSITION_TRACKING_DEADBAND = 10`
- Rationale: Allow larger position error before correction

### Option 3: Add Velocity Smoothing
- Add exponential moving average to smooth velocity command
- Filter out sudden speed changes

### Option 4: Reduce Controller Gain
- Modify `_approach()` function to use gentler acceleration
- Currently: `max_delta = self.max_speed_hz * elapsed_s * blend_factor`
- Try: Reduce blend_factor or add damping

### Option 5: Position Feedback Filter
- Add low-pass filter to position error
- Smooth out noise before controller reaction

## Recommended Next Steps

1. **Capture baseline** with current settings (DONE)
2. **Try Option 1**: Reduce `LINEAR_FADE_CHUNK_STEPS` to 2
3. **Capture and compare** - does it reduce shoot magnitude?
4. **Iterate** with different chunk sizes and deadband values
5. **Validate** with full 2-min capture and stdev analysis

## Files Modified

- `firmware/config.py` - Linear fade parameters
- `firmware/main.py` - ChunkedPositionController with fade detection
- `hil/opencv_streamer/streamer.py` - Added timestamps to TCP output

## Capture Data

- `hil/captures/x_data_2min_timestamped.txt` - Raw timestamped X data
- `hil/captures/x_data_2min_timestamped_analysis.png` - Full timeline plot
- `hil/captures/micro_shoots_analysis.png` - Individual shoot analysis
- `hil/captures/x_data_ultra_zoom.png` - Deviation from ideal line
