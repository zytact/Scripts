#!/bin/sh

while true
do
	sleep 13m
	laptop1_price=$(node ~/Code/price-tracker/main.js https://www.flipkart.com/acer-aspire-3-ryzen-5-quad-core-3500u-8-gb-512-gb-ssd-windows-10-home-a315-23-notebook/p/itm96a9ff5907861)
	laptop1="Acer Aspire 3 Ryzen 5 Quad Core 3500U"
	laptop2_price=$(node ~/Code/price-tracker/main.js https://www.flipkart.com/acer-aspire-5-core-i5-11th-gen-8-gb-1-tb-hdd-256-gb-ssd-windows-10-home-a515-56-thin-light-laptop/p/itmdf83f58e7a903)
	laptop2="Acer Aspire 5 Core i5 11th Gen"

	notify-send -t 10000 -u "normal" "$laptop1" "$laptop1_price"
	notify-send -t 10000 -u "normal" "$laptop2" "$laptop2_price"
	paplay /usr/share/sounds/freedesktop/stereo/suspend-error.oga

	sleep 47m
done
