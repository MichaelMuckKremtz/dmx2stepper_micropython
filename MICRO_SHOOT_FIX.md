# Micro-Shoot Fix: Resolved with DirectDMXController

## Issue Description (RESOLVED)

During linear fades, the motor exhibited **periodic micro-shoots** - sudden drops of 5-14px followed by quick recovery (40-80ms). These created a jagged, non-smooth appearance in the motion path.

### Root Cause (IDENTIFIED)

The micro-shoots were caused by **controller hunting** in ChunkedPositionController:

1. **Acceleration limiting**: Motor couldn't track DMX rate exactly
2. **Position overshoot**: Motor overshoots target, controller corrects
3. **Chunk-based motion**: Discrete motion bursts created hunting pattern
4. **Tracking deadband**: `POSITION_TRACKING_DEADBAND=5` triggered corrections

## Solution: DirectDMXController

Replaced `ChunkedPositionController` with `DirectDMXController` that follows DMX directly without acceleration limiting.

### Key Changes

1. **No acceleration limiting**: Motor moves at constant speed to match DMX position
2. **No tracking deadband**: Always moves to match DMX target
3. **Direct position tracking**: Calculates steps needed and moves directly
4. **High-speed blocking moves**: Uses full motor speed for tracking

### New Configuration (config.py)
```python
# Linear fade detection
LINEAR_FADE_WINDOW = 16
LINEAR_FADE_VARIANCE_THRESH = 5.0
LINEAR_FADE_MIN_STEPS_SEC = 30

# Flat movement (direct DMX-following)
FLAT_MOVE_SPEED_HZ = 18684  # Max speed for direct tracking
USE_DIRECT_CONTROLLER = True  # Use DirectDMXController

# Legacy ChunkedPositionController (backup)
LINEAR_FADE_CHUNK_STEPS = 4
LINEAR_FADE_BLEND_MS = 200
VELOCITY_DEADBAND_HZ = 500
POSITION_TRACKING_DEADBAND = 5
RUNTIME_MAX_CHUNK_STEPS = 64
RUNTIME_MIN_CHUNK_SPEED_HZ = 500
```

### Expected Results

- **No micro-shoots**: Direct tracking eliminates hunting
- **Flat movement**: Motor follows DMX like a direct slave
- **Consistent smoothness**: No acceleration/deceleration artifacts

## Files Modified

- `firmware/config.py` - Added `FLAT_MOVE_SPEED_HZ`, `USE_DIRECT_CONTROLLER`
- `firmware/main.py` - Added `DirectDMXController` class, conditional controller selection

## Verification

Test with:
```bash
cd firmware && ./deploy.sh && mpremote reset
nc localhost 9999 > x_data_flat.txt
```

Compare stdev during fades - should be near 0 for truly flat movement.
