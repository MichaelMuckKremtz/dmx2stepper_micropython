#!/bin/bash
cd "$(dirname "$0")/hil" && python3 capture.py "${1:-180}" "$2"
