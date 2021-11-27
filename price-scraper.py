#!/usr/bin/env python3

import requests
from bs4 import BeautifulSoup
import sys

headers = requests.utils.default_headers()
headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64; rv:70.0) Gecko/20100101 Firefox/70.0'
})

argv = sys.argv
resp = requests.get(argv[1])
soup = BeautifulSoup(resp.text, "html.parser")
price_tag_set = soup.select("#container > div > div._2c7YLP.UtUXW0._6t1WkM._3HqJxg > div._1YokD2._2GoDe3 > div._1YokD2._3Mn1Gg.col-8-12 > div:nth-child(2) > div > div.dyC4hf > div.CEmiEU > div > div._30jeq3._16Jk6d")
for price_tag in price_tag_set:
    print(price_tag.text) 