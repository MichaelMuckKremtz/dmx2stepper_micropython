#!/usr/bin/env python3
"""Capture X position data and generate analysis graph."""

import subprocess
import sys
import os
from datetime import datetime

DEFAULT_DURATION = 180
TCP_PORT = 9999
CAPTURES_DIR = os.path.join(os.path.dirname(__file__), 'captures')

def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DURATION
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    txt_file = os.path.join(CAPTURES_DIR, f'{timestamp}_{duration}s_capture.txt')
    analysis_file = os.path.join(CAPTURES_DIR, f'{timestamp}_{duration}s_capture_analysis.png')
    
    print(f'Capturing {duration}s of data...')
    print(f'Output: {txt_file}')
    
    try:
        result = subprocess.run(
            ['timeout', str(duration), 'nc', 'localhost', str(TCP_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=duration + 10
        )
        with open(txt_file, 'wb') as f:
            f.write(result.stdout)
    except subprocess.TimeoutExpired:
        print('Capture timed out')
        return 1
    except Exception as e:
        print(f'Capture failed: {e}')
        return 1
    
    with open(txt_file, 'r') as f:
        lines = len(f.read().strip().split('\n'))
    print(f'Captured {lines} points')
    
    print(f'Generating analysis...')
    try:
        subprocess.run([
            'python3', 'analyze_x_data.py', txt_file, analysis_file
        ], check=True)
        print(f'Analysis saved: {analysis_file}')
    except Exception as e:
        print(f'Analysis failed: {e}')
        return 1
    
    print('Done!')
    return 0

if __name__ == '__main__':
    sys.exit(main())
