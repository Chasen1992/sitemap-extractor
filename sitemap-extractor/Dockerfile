# A plain Python program. No browser, no headless Chrome, no Playwright.
#
# This Actor reads sitemap files that a website has already published and
# probes the status codes of the URLs it finds there. That is the whole job.
# A generic crawler needs a browser image and several gigabytes of RAM to do
# the same thing less accurately, which is why a run of this Actor is cheap.
FROM apify/actor-python:3.12

WORKDIR /usr/src/app

# Dependencies in their own layer, so editing the source does not re-resolve
# them on every build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# GPL-3.0-or-later: recipients of this image must receive the licence text and
# the attribution for ultimate-sitemap-parser, the GPL-3.0 library this Actor
# is a derivative work of. Shipping them in the image is what makes that true
# for a binary/container distribution and not only for a source release.
COPY LICENSE ./LICENSE
COPY NOTICE ./NOTICE

# `python src/main.py` rather than `python -m src.main`: the script's own
# directory lands on sys.path, which is what makes `from sitemap_core import`
# resolve to the vendored package in src/ without a PYTHONPATH setting.
CMD ["python", "src/main.py"]
