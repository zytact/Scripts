#!/usr/bin/env bash
[ -z "$5" ] && echo "Usage: $0 <image> <x> <y> <max_height> <max_width>" && exit
source "/usr/local/lib/python3.8/dist-packages/ueberzug/lib/lib.sh"
ImageLayer 0< <(
	ImageLayer::add [identifier]="example0" [x]="$1" [y]="$2" [max_height]="$3" [max_width]="$4" [path]="$5"
	read
)
