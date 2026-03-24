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
    
    hold_threshold = 20
    hold_regions = []
    i = 0
    while i < len(velocities):
        if abs(velocities[i][1]) < hold_threshold:
            t_start = velocities[i][0]
            while i < len(velocities) and abs(velocities[i][1]) < hold_threshold:
                i += 1
            t_end = velocities[i-1][0] if i > 0 else t_start
            if t_end > t_start + 0.5:
                hold_regions.append((t_start, t_end))
        else:
            i += 1
    
    fade_regions = []
    i = 0
    while i < len(velocities):
        if abs(velocities[i][1]) >= hold_threshold:
            t_start = velocities[i][0]
            while i < len(velocities) and abs(velocities[i][1]) >= hold_threshold:
                i += 1
            t_end = velocities[i-1][0] if i > 0 else t_start
            fade_regions.append((t_start, t_end))
        else:
            i += 1
    
    return {
        'times': times,
        'values': values,
        'velocities': velocities,
        'hold_regions': hold_regions,
        'fade_regions': fade_regions,
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
        print("Usage: python3 analyze_x_data.py <data_file> [output_png]")
        sys.exit(1)
    
    filename = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(filename), os.path.basename(filename).replace('.txt', '_analysis.png'))
    
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
    print(f"Fade regions: {len(analysis['fade_regions'])}")
    print(f"Hold regions (>0.5s): {len(analysis['hold_regions'])}")
    
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
    
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.ticker import MultipleLocator
        
        fig = plt.figure(figsize=(18, 14))
        
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
        ax1 = fig.add_subplot(3, 1, 1)
        ax1.plot(times, values, 'b-', linewidth=1, alpha=0.9)

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
        
        ax1.set_title(f'X Position vs Time\n{firmware_info} | {len(data)} points | X range: {max(values)-min(values)} px', fontsize=12)
        
        # Legend
        green_patch = mpatches.Patch(color='green', alpha=0.15, label='Fade')
        red_patch = mpatches.Patch(color='red', alpha=0.15, label='Hold')
        ax1.legend(handles=[green_patch, red_patch], loc='upper right')
        
        # 2. Velocity plot
        ax2 = fig.add_subplot(3, 1, 2)
        v_times = [v[0] for v in analysis['velocities']]
        v_vals = [v[1] for v in analysis['velocities']]
        ax2.plot(v_times, v_vals, 'r-', linewidth=0.5, alpha=0.7)
        ax2.axhline(y=0, color='black', linestyle='-', alpha=0.4)
        ax2.axhline(y=20, color='gray', linestyle='--', alpha=0.4)
        ax2.axhline(y=-20, color='gray', linestyle='--', alpha=0.4)
        
        ax2.xaxis.set_major_locator(MultipleLocator(t_major))
        ax2.yaxis.set_major_locator(MultipleLocator(50))
        ax2.grid(True, which='major', color=grid_color, linestyle='-', linewidth=0.5)
        ax2.grid(True, which='minor', color=fine_grid_color, linestyle='-', linewidth=0.3)
        
        ax2.set_xlabel('Time (s)', fontsize=11)
        ax2.set_ylabel('Velocity (px/s)', fontsize=11)
        ax2.set_title('Movement Velocity', fontsize=12)
        
        # 3. Zoomed fade detail
        ax3 = fig.add_subplot(3, 1, 3)
        
        # Show first significant fade with fine detail
        significant_fades = [s for s in fade_stats if s['duration'] > 2]
        if significant_fades:
            stats = significant_fades[0]
            t0, t1 = stats['start_t'], stats['end_t']
            margin = stats['duration'] * 0.1
            
            fade_data = [(t, x) for t, x in data if t0 - margin <= t <= t1 + margin]
            if fade_data:
                f_times = [t for t, x in fade_data]
                f_vals = [x for t, x in fade_data]
                
                ax3.plot(f_times, f_vals, 'b-', linewidth=1.5, label='Actual Position')
                
                # Ideal linear path
                t_range = [t0, t1]
                x_range = [stats['start_x'], stats['end_x']]
                ax3.plot(t_range, x_range, 'r--', linewidth=2, label='Ideal Linear Path', alpha=0.8)
                
                # Deviation shading
                ideal_slope = stats['velocity']
                deviations = [x - (stats['start_x'] + ideal_slope * (t - t0)) for t, x in fade_data if t0 <= t <= t1]
                if deviations:
                    dev_text = f'Deviation: avg={sum(deviations)/len(deviations):.1f}px, max={max(abs(d) for d in deviations):.1f}px'
                    ax3.text(0.02, 0.98, dev_text, transform=ax3.transAxes, fontsize=10,
                            verticalalignment='top', bbox=dict(boxstyle='round', facecolor='yellow', alpha=0.7))
                
                ax3.xaxis.set_major_locator(MultipleLocator(0.5))
                ax3.yaxis.set_major_locator(MultipleLocator(10))
                ax3.grid(True, which='major', color=grid_color, linestyle='-', linewidth=0.5)
                ax3.grid(True, which='minor', color=fine_grid_color, linestyle='-', linewidth=0.3)
                
                ax3.set_xlabel('Time (s)', fontsize=11)
                ax3.set_ylabel('X Position (px)', fontsize=11)
                ax3.set_title(f'Fade Detail: {stats["start_x"]} -> {stats["end_x"]} px over {stats["duration"]:.1f}s (stdev={stats["stdev"]:.1f}px)', fontsize=12)
                ax3.legend(loc='lower right')
        
        for ax in [ax1, ax2]:
            ax.tick_params(axis='x', rotation=45)

        plt.tight_layout()
        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        print(f"\nSaved visualization to {output_file}")
        
    except ImportError as e:
        print(f"matplotlib not available: {e}")

if __name__ == '__main__':
    main()
