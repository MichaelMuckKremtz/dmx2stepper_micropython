#!/usr/bin/env python3
"""Compare position accuracy across homing speed test captures."""

import json
import math
import os
import sys


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


def find_hold_regions(data, x_tolerance=5, min_duration=1.0, min_points=3):
    """Find stationary hold regions by grouping consecutive points with similar x.

    The streamer only sends data when x changes, so data is sparse during holds.
    Velocity-based detection fails here. Instead, group consecutive points whose
    x stays within ±x_tolerance of the running mean, and keep groups lasting
    at least min_duration seconds.
    """
    if len(data) < min_points:
        return []

    holds = []
    current_xs = [data[0][1]]
    current_t_start = data[0][0]

    for i in range(1, len(data)):
        t, x = data[i]
        mean_x = sum(current_xs) / len(current_xs)
        if abs(x - mean_x) <= x_tolerance:
            current_xs.append(x)
        else:
            duration = data[i - 1][0] - current_t_start
            if duration >= min_duration and len(current_xs) >= min_points:
                holds.append(sum(current_xs) / len(current_xs))
            current_xs = [x]
            current_t_start = t

    # Last region
    if len(current_xs) >= min_points:
        duration = data[-1][0] - current_t_start
        if duration >= min_duration:
            holds.append(sum(current_xs) / len(current_xs))

    return holds


def cluster_left_right(hold_means):
    """Cluster hold positions into left and right groups by largest gap."""
    if len(hold_means) < 2:
        return [], []

    sorted_vals = sorted(hold_means)
    max_gap = 0
    max_gap_idx = 1
    for i in range(1, len(sorted_vals)):
        gap = sorted_vals[i] - sorted_vals[i - 1]
        if gap > max_gap:
            max_gap = gap
            max_gap_idx = i

    if max_gap < 20:
        return hold_means, []

    threshold = (sorted_vals[max_gap_idx - 1] + sorted_vals[max_gap_idx]) / 2
    left = [x for x in hold_means if x < threshold]
    right = [x for x in hold_means if x >= threshold]
    return left, right


def std_dev(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def analyze_speed(capture_file):
    """Analyze a single speed's capture file and return metrics."""
    data = parse_data(capture_file)
    if len(data) < 20:
        return None

    all_holds = find_hold_regions(data)

    if len(all_holds) < 2:
        return None

    left, right = cluster_left_right(all_holds)

    left_mean = sum(left) / len(left) if left else 0
    right_mean = sum(right) / len(right) if right else 0
    left_std = std_dev(left) if len(left) >= 2 else 0
    right_std = std_dev(right) if len(right) >= 2 else 0
    combined_std = math.sqrt(left_std ** 2 + right_std ** 2)

    return {
        'n_holds': len(all_holds),
        'left_values': left,
        'right_values': right,
        'left_mean': round(left_mean, 2),
        'right_mean': round(right_mean, 2),
        'left_std': round(left_std, 2),
        'right_std': round(right_std, 2),
        'combined_std': round(combined_std, 2),
        'span': round(right_mean - left_mean, 2) if left and right else 0,
    }


def generate_comparison(results, output_png, output_json, run_dir):
    """Generate comparison visualization and summary."""
    # Save JSON summary
    json_data = {}
    for speed, metrics in sorted(results.items()):
        json_data[speed] = {k: v for k, v in metrics.items()
                           if k not in ('left_values', 'right_values')}
    with open(output_json, 'w') as f:
        json.dump(json_data, f, indent=2)
    print(f'Saved summary: {output_json}')

    # Console summary
    print(f'\n{"Speed":>6} {"Hz":>7} {"Holds":>6} '
          f'{"L mean":>7} {"L std":>6} {"R mean":>7} {"R std":>6} '
          f'{"Comb":>6} {"Span":>7}')
    print('-' * 72)
    for speed in sorted(results.keys()):
        m = results[speed]
        actual_hz = speed * 16
        print(f'{speed:>6} {actual_hz:>7} {m["n_holds"]:>6} '
              f'{m["left_mean"]:>7.1f} {m["left_std"]:>6.2f} '
              f'{m["right_mean"]:>7.1f} {m["right_std"]:>6.2f} '
              f'{m["combined_std"]:>6.2f} {m["span"]:>7.1f}')

    # Find best speed
    best_speed = min(results.keys(), key=lambda s: results[s]['combined_std'])
    print(f'\nBest speed: {best_speed} (combined std = {results[best_speed]["combined_std"]:.2f} px)')

    # Matplotlib visualization
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MultipleLocator
    except ImportError:
        print('matplotlib not available, skipping visualization')
        return

    speeds_sorted = sorted(results.keys())
    fig, axes = plt.subplots(3, 1, figsize=(16, 16))

    # Panel 1: Scatter plot of hold positions per speed
    ax1 = axes[0]
    for speed in speeds_sorted:
        m = results[speed]
        jitter = 0
        if m['left_values']:
            ax1.scatter([speed + jitter] * len(m['left_values']), m['left_values'],
                       c='steelblue', alpha=0.6, s=30, zorder=3)
        if m['right_values']:
            ax1.scatter([speed + jitter] * len(m['right_values']), m['right_values'],
                       c='indianred', alpha=0.6, s=30, zorder=3)
        if m['left_values']:
            ax1.plot([speed - 8, speed + 8], [m['left_mean']] * 2,
                    'b-', linewidth=2, alpha=0.8)
        if m['right_values']:
            ax1.plot([speed - 8, speed + 8], [m['right_mean']] * 2,
                    'r-', linewidth=2, alpha=0.8)

    ax1.set_xlabel('Homing Speed (1/8 Hz base)', fontsize=11)
    ax1.set_ylabel('X Position (px)', fontsize=11)
    ax1.set_title('Hold Positions per Homing Speed (blue=left, red=right)', fontsize=13)
    ax1.set_xticks(speeds_sorted)
    ax1.grid(True, alpha=0.3)

    # Panel 2: Std deviation vs speed
    ax2 = axes[1]
    left_stds = [results[s]['left_std'] for s in speeds_sorted]
    right_stds = [results[s]['right_std'] for s in speeds_sorted]
    combined_stds = [results[s]['combined_std'] for s in speeds_sorted]

    ax2.plot(speeds_sorted, left_stds, 'bo-', label='Left std', markersize=6)
    ax2.plot(speeds_sorted, right_stds, 'ro-', label='Right std', markersize=6)
    ax2.plot(speeds_sorted, combined_stds, 'ko-', label='Combined std', markersize=8, linewidth=2)

    # Highlight best
    ax2.axvline(x=best_speed, color='green', linestyle='--', alpha=0.5, label=f'Best: {best_speed}')

    ax2.set_xlabel('Homing Speed (1/8 Hz base)', fontsize=11)
    ax2.set_ylabel('Position Std Dev (px)', fontsize=11)
    ax2.set_title('Position Repeatability vs Homing Speed (lower = better)', fontsize=13)
    ax2.set_xticks(speeds_sorted)
    ax2.legend(loc='upper right')
    ax2.grid(True, alpha=0.3)

    # Panel 3: Summary table
    ax3 = axes[2]
    ax3.axis('off')
    headers = ['Speed', 'Actual Hz', 'Holds',
               'L mean', 'L std', 'R mean', 'R std', 'Combined', 'Span']
    table_data = []
    cell_colors = []
    for speed in speeds_sorted:
        m = results[speed]
        row = [
            str(speed), str(speed * 16), str(m['n_holds']),
            f'{m["left_mean"]:.1f}', f'{m["left_std"]:.2f}',
            f'{m["right_mean"]:.1f}', f'{m["right_std"]:.2f}',
            f'{m["combined_std"]:.2f}', f'{m["span"]:.1f}',
        ]
        table_data.append(row)
        if speed == best_speed:
            cell_colors.append(['#d4edda'] * len(headers))
        else:
            cell_colors.append(['white'] * len(headers))

    table = ax3.table(cellText=table_data, colLabels=headers,
                      cellColours=cell_colors,
                      loc='center', cellLoc='center')
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.0, 1.5)

    ax3.set_title(f'Summary (best: speed {best_speed}, combined std = {results[best_speed]["combined_std"]:.2f} px)',
                  fontsize=13, pad=20)

    plt.tight_layout()
    plt.savefig(output_png, dpi=150, bbox_inches='tight')
    print(f'Saved visualization: {output_png}')


def main():
    if len(sys.argv) < 2:
        print('Usage: python3 compare_homing_speeds.py <run_directory>')
        sys.exit(1)

    run_dir = sys.argv[1]
    if not os.path.isdir(run_dir):
        print(f'Error: {run_dir} is not a directory')
        sys.exit(1)

    # Find all speed_* subdirectories
    speed_dirs = sorted([
        d for d in os.listdir(run_dir)
        if d.startswith('speed_') and os.path.isdir(os.path.join(run_dir, d))
    ])

    if not speed_dirs:
        print(f'No speed_* directories found in {run_dir}')
        sys.exit(1)

    print(f'Found {len(speed_dirs)} speed directories')

    results = {}
    for sd in speed_dirs:
        speed = int(sd.replace('speed_', ''))
        capture_file = os.path.join(run_dir, sd, 'capture.txt')

        if not os.path.exists(capture_file):
            print(f'  {sd}: no capture.txt, skipping')
            continue

        metrics = analyze_speed(capture_file)
        if metrics is None:
            print(f'  {sd}: insufficient data, skipping')
            continue

        results[speed] = metrics
        print(f'  {sd}: {metrics["n_holds"]} holds, '
              f'combined_std={metrics["combined_std"]:.2f} px')

    if len(results) < 2:
        print('Need at least 2 successful speed captures for comparison')
        sys.exit(1)

    output_png = os.path.join(run_dir, 'comparison_analysis.png')
    output_json = os.path.join(run_dir, 'summary.json')
    generate_comparison(results, output_png, output_json, run_dir)


if __name__ == '__main__':
    main()
