#!/bin/bash
# GCE startup script: detect 30-40 ft piles on every Feb 2026 view tile, render their 3-D views,
# upload, power off. Same region as the bucket (us-west1) so reading the tiles costs nothing.
#   gcloud compute instances create pile-detect-22core --zone us-west1-a --machine-type c3-standard-22 \
#     --image-family debian-12 --image-project debian-cloud --boot-disk-size 100GB --scopes cloud-platform \
#     --max-run-duration 3h --instance-termination-action DELETE \
#     --metadata-from-file startup-script=tools/pile_detect_vm.sh
# Inputs:  $W/in/ (pilefind.py, pile_detect.py, pile_views.py, train_rows.json, piles.json, units.json),
#          the view tiles in $TILES
# Outputs: $W/new_piles.json, $W/out/ (per-tile detections, unmapped 40 ft+ list), $W/views/pile_<id>.webp,
#          $W/job.log, $W/DONE (exit code of the detection)
W=gs://aso-august-w1/pc_work_10212026/pile_detect
TILES=gs://aso-august-w1/pc_work_10212026/pike_view2/tiles
exec > >(tee /var/log/pilejob.log > /dev/ttyS0) 2>&1
(while true; do gcloud storage cp -q /var/log/pilejob.log $W/job.log || echo "pilejob: log upload failed"; sleep 30; done) &
echo "pilejob: start $(date -u)"
mkdir -p /mnt/w && cd /mnt/w
apt-get update -qq && apt-get install -y -qq bzip2
curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj bin/micromamba
rm -rf /mnt/w/env out views views_fetch
MAMBA_ROOT_PREFIX=/mnt/w/mamba ./bin/micromamba create -y -q -p /mnt/w/env -c conda-forge \
  "pdal>=2.10" python-pdal numpy scipy matplotlib laspy lazrs-python pyproj pillow rasterio \
  scikit-learn scikit-image contourpy
echo "pilejob: env ready $(date -u)"
gcloud storage cp "$W/in/*" .
gcloud storage rsync -r $TILES tiles
echo "pilejob: inputs ready $(date -u): $(ls tiles | wc -l) tiles"
# startup scripts run without HOME, which matplotlib needs for its config dir
export HOME=/mnt/w MPLCONFIGDIR=/mnt/w/mpl GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif
WORKERS=$(nproc) ./env/bin/python pile_detect.py train_rows.json piles.json units.json tiles out 2> >(grep -v -E 'Warning|nanmedian' >&2)
rc=$?
./env/bin/python pile_detect.py --merge piles.json out new_piles.json
echo "pilejob: detection rc=$rc $(date -u)"
# 3-D views of the additions: 2022 ground per pile first, then the render
FETCH_ONLY=1 GROUND_DIR=/mnt/w/ground22 WORKERS=$(nproc) ./env/bin/python pile_views.py new_piles.json tiles views_fetch 2> >(grep -v 'PDAL Error' >&2)
GROUND_DIR=/mnt/w/ground22 WORKERS=$(nproc) ./env/bin/python pile_views.py new_piles.json tiles views 2> >(grep -v 'PDAL Error' >&2)
echo "pilejob: views $(ls views/*.webp 2>/dev/null | wc -l) $(date -u)"
gcloud storage rsync -r out $W/out
gcloud storage rsync -r views $W/views
gcloud storage cp new_piles.json $W/
gcloud storage rsync -r ground22 gs://aso-august-w1/pc_work_10212026/pile_views/ground22
gcloud storage cp -q /var/log/pilejob.log $W/job.log
echo $rc | gcloud storage cp - $W/DONE && echo "pilejob: DONE written"
sleep 5
poweroff
