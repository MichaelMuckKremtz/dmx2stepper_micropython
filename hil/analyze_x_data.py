#!/usr/bin/env python3
"""Analyze and visualize X position data from DMX-to-Stepper test with fine grid."""

import sys
import math
import os

def parse_data(filename):
    """Parse data file with format 'x,time'."""
    data = []
    with open(filename, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line == 'null':
                continue
            parts = line.split(',')
            try:
                if len(parts) == 2:
                    x = int(parts[0])
                    t = float(parts[1])
                    data.append((t, x))
            except ValueError:
                continue
    return data

def detect_startup_phase(data, velocities):
    """Detect endstop events by velocity spikes below -1300 px/s.
    Returns list of (start_t, end_t) excluded regions."""
    if len(velocities) < 2:
        return []
    
    excluded = []
    VELOCITY_THRESHOLD = -1300
    PRE_MARGIN = 0.5
    POST_MARGIN = 3.0
    
    for t, v in velocities:
        if v <= VELOCITY_THRESHOLD:
            excluded.append((t - PRE_MARGIN, t + POST_MARGIN))
    
    # Merge overlapping regions
    if not excluded:
        return []
    
    excluded.sort()
    merged = [excluded[0]]
    for start, end in excluded[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    
    return merged

def analyze_movement(data):
    """Analyze movement patterns."""
    if len(data) < 2:
        return {}
    
    times = [t for t, x in data]
    values = [x for t, x in data]
    
    velocities = []
    for i in range(1, len(data)):
        dt = data[i][0] - data[i-1][0]
        if dt > 0.001:
            vel = (data[i][1] - data[i-1][1]) / dt
            velocities.append((data[i][0], vel))
    
    # Detect endstop events
    excluded_regions = detect_startup_phase(data, velocities)
    
    # Filter velocities to exclude endstop regions
    def in_excluded(t):
        return any(s <= t <= e for s, e in excluded_regions)
    
    fade_velocities = [(t, v) for t, v in velocities if not in_excluded(t)]
    
    # Build fade regions from consecutive non-excluded velocities
    fade_regions = []
    for t, v in fade_velocities:
        if not fade_regions or t - fade_regions[-1][1] > 0.1:
            fade_regions.append((t, t))
        else:
            fade_regions[-1] = (fade_regions[-1][0], t)
    
    return {
        'times': times,
        'values': values,
        'velocities': velocities,
        'fade_regions': fade_regions,
        'excluded_regions': excluded_regions,
    }

def calculate_velocity_stability(velocities, fade_region):
    """Calculate velocity stability metrics for a movement region."""
    t0, t1 = fade_region
    region_vels = [v for t, v in velocities if t0 <= t <= t1]
    
    if len(region_vels) < 3:
        return None
    
    mean_v = sum(region_vels) / len(region_vels)
    variance = sum((v - mean_v) ** 2 for v in region_vels) / len(region_vels)
    stdev = math.sqrt(variance)
    max_dev = max(abs(v - mean_v) for v in region_vels)
    
    # Jitter: average absolute velocity change between consecutive samples
    deltas = [abs(region_vels[i] - region_vels[i-1]) for i in range(1, len(region_vels))]
    avg_jitter = sum(deltas) / len(deltas) if deltas else 0
    max_jitter = max(deltas) if deltas else 0
    
    # Coefficient of variation (normalized stability)
    cv = (stdev / abs(mean_v) * 100) if abs(mean_v) > 1 else 0
    
    return {
        'start_t': t0, 'end_t': t1,
        'mean_vel': mean_v,
        'stdev': stdev,
        'max_dev': max_dev,
        'avg_jitter': avg_jitter,
        'max_jitter': max_jitter,
        'cv_pct': cv,
    }

def calculate_fade_stats(data, fade_region):
    """Calculate statistics for a fade region."""
    t0, t1 = fade_region
    fade_data = [(t, x) for t, x in data if t0 <= t <= t1]
    
    if len(fade_data) < 3:
        return None
    
    x0 = fade_data[0][1]
    x1 = fade_data[-1][1]
    dt = t1 - t0
    dx = x1 - x0
    
    if abs(dx) < 10 or dt <= 0:
        return None
    
    ideal_slope = dx / dt
    residuals = []
    for t, x in fade_data:
        ideal_x = x0 + ideal_slope * (t - t0)
        residuals.append(x - ideal_x)
    
    stdev = math.sqrt(sum(r*r for r in residuals) / len(residuals))
    max_dev = max(abs(r) for r in residuals)
    
    return {
        'start_t': t0, 'end_t': t1,
        'start_x': x0, 'end_x': x1,
        'duration': dt, 'delta_x': dx,
        'velocity': ideal_slope,
        'stdev': stdev, 'max_dev': max_dev,
    }

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_x_data.py <data_file> [output_png] [label]")
        sys.exit(1)
    
    filename = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(filename), os.path.basename(filename).replace('.txt', '_analysis.png'))
    label = sys.argv[3] if len(sys.argv) > 3 else None
    
    data = parse_data(filename)
    print(f"Loaded {len(data)} data points")
    
    if not data:
        print("No valid data found")
        sys.exit(1)
    
    analysis = analyze_movement(data)
    times = analysis['times']
    values = analysis['values']
    
    print(f"\n=== Analysis Results ===")
    print(f"Time range: {min(times):.2f}s - {max(times):.2f}s")
    print(f"X range: {min(values)} - {max(values)} ({max(values)-min(values)} px)")
    
    excluded_regions = analysis.get('excluded_regions', [])
    if excluded_regions:
        total_excluded = sum(e - s for s, e in excluded_regions)
        print(f"Endstop exclusions: {len(excluded_regions)} regions ({total_excluded:.1f}s total)")
        for s, e in excluded_regions:
            print(f"  {s:.2f}s - {e:.2f}s ({e-s:.1f}s)")
    
    print(f"Fade regions: {len(analysis['fade_regions'])}")
    
    fade_stats = []
    for fade in analysis['fade_regions']:
        stats = calculate_fade_stats(data, fade)
        if stats:
            fade_stats.append(stats)
    
    if fade_stats:
        avg_stdev = sum(s['stdev'] for s in fade_stats) / len(fade_stats)
        avg_max_dev = sum(s['max_dev'] for s in fade_stats) / len(fade_stats)
        print(f"\nFade Quality (n={len(fade_stats)}):")
        print(f"  Avg stdev: {avg_stdev:.2f} px")
        print(f"  Avg max dev: {avg_max_dev:.2f} px")
    
    # Velocity stability metrics
    vel_stability = []
    for fade in analysis['fade_regions']:
        vs = calculate_velocity_stability(analysis['velocities'], fade)
        if vs:
            vel_stability.append(vs)
    
    if vel_stability:
        avg_v_stdev = sum(s['stdev'] for s in vel_stability) / len(vel_stability)
        avg_v_jitter = sum(s['avg_jitter'] for s in vel_stability) / len(vel_stability)
        max_v_jitter = max(s['max_jitter'] for s in vel_stability)
        avg_cv = sum(s['cv_pct'] for s in vel_stability) / len(vel_stability)
        print(f"\nVelocity Stability (n={len(vel_stability)}):")
        print(f"  Avg stdev: {avg_v_stdev:.1f} px/s")
        print(f"  Avg jitter: {avg_v_jitter:.1f} px/s")
        print(f"  Max jitter: {max_v_jitter:.1f} px/s")
        print(f"  Avg CV: {avg_cv:.1f}%")
    
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.ticker import MultipleLocator
        
        fig = plt.figure(figsize=(18, 10))
        
        # Fine grid styling
        grid_color = '#cccccc'
        fine_grid_color = '#eeeeee'

        # Choose readable time tick interval based on duration
        t_span = max(times) - min(times) if times else 1
        if t_span > 120:
            t_major = 30
        elif t_span > 60:
            t_major = 10
        elif t_span > 20:
            t_major = 5
        else:
            t_major = 1

        # 1. Main position plot with fine grid
        ax1 = fig.add_subplot(2, 1, 1)
        ax1.plot(times, values, 'b-', linewidth=1, alpha=0.9)

        # Shade excluded reset regions
        excluded_regions = analysis.get('excluded_regions', [])
        for idx, (ex_s, ex_e) in enumerate(excluded_regions):
            label = 'Endstop (excluded)' if idx == 0 else None
            ax1.axvspan(ex_s, ex_e, color='gray', alpha=0.3, label=label)

        ax1.xaxis.set_major_locator(MultipleLocator(t_major))
        ax1.yaxis.set_major_locator(MultipleLocator(50))
        ax1.grid(True, which='major', color=grid_color, linestyle='-', linewidth=0.5)
        ax1.grid(True, which='minor', color=fine_grid_color, linestyle='-', linewidth=0.3)
        
        ax1.set_xlabel('Time (s)', fontsize=11)
        ax1.set_ylabel('X Position (px)', fontsize=11)
        
        # Get firmware info from filename
        firmware_info = "ChunkedPositionController"
        if 'direct' in filename.lower():
            firmware_info = "DirectDMXController"
        
        title = f'X Position vs Time\n{firmware_info} | {len(data)} points | X range: {max(values)-min(values)} px'
        if label:
            title += f'\n[{label}]'
        ax1.set_title(title, fontsize=12)
        
        # Legend
        handles = []
        if excluded_regions:
            handles.append(mpatches.Patch(color='gray', alpha=0.3, label=f'Endstop (excluded, {len(excluded_regions)})'))
        ax1.legend(handles=handles, loc='upper right')
        
        # 2. Velocity plot - shared x-axis with position
        ax2 = fig.add_subplot(2, 1, 2, sharex=ax1)
        v_times = [v[0] for v in analysis['velocities']]
        v_vals = [v[1] for v in analysis['velocities']]
        ax2.plot(v_times, v_vals, 'r-', linewidth=0.5, alpha=0.7)
        ax2.axhline(y=0, color='black', linestyle='-', alpha=0.4)
        ax2.axhline(y=20, color='gray', linestyle='--', alpha=0.4)
        ax2.axhline(y=-20, color='gray', linestyle='--', alpha=0.4)
        
        # Shade excluded regions on velocity plot
        for ex_s, ex_e in excluded_regions:
            ax2.axvspan(ex_s, ex_e, color='gray', alpha=0.3)
        
        ax2.xaxis.set_major_locator(MultipleLocator(t_major))
        ax2.yaxis.set_major_locator(MultipleLocator(50))
        ax2.grid(True, which='major', color=grid_color, linestyle='-', linewidth=0.5)
        ax2.grid(True, which='minor', color=fine_grid_color, linestyle='-', linewidth=0.3)
        
        ax2.set_xlabel('Time (s)', fontsize=11)
        ax2.set_ylabel('Velocity (px/s)', fontsize=11)
        ax2.set_title('Movement Velocity', fontsize=12)
        
        for ax in [ax1, ax2]:
            ax.tick_params(axis='x', rotation=45)

        plt.tight_layout()
        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        print(f"\nSaved visualization to {output_file}")
        
    except ImportError as e:
        print(f"matplotlib not available: {e}")

if __name__ == '__main__':
    main()
