#!/bin/bash
# GCE startup script: cut view tiles for the NE Sleighville strip (flown in Feb 2026, north of the old
# analysis extent), run the pile detection there, render the new piles, upload, power off.
# Inputs in $W/in: extend_tiles.py, pilefind.py, pile_detect.py, pile_views.py, train_rows.json, piles.json, units.json.
W=gs://aso-august-w1/pc_work_10212026/pile_detect_ne

RAW=gs://aso-august-w1/20260203/CA_acqYU_20260203_smrflas_ti-7680_tj-2400_ts60_r1448.las
OLD=gs://aso-august-w1/pc_work_10212026/pike_view2/tiles
exec > >(tee /var/log/pilejob.log > /dev/ttyS0) 2>&1
(while true; do gcloud storage cp -q /var/log/pilejob.log $W/job.log || echo "pilejob: log upload failed"; sleep 30; done) &
echo "pilejob: start $(date -u)"
mkdir -p /mnt/w && cd /mnt/w
apt-get update -qq && apt-get install -y -qq bzip2
curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj bin/micromamba
MAMBA_ROOT_PREFIX=/mnt/w/mamba ./bin/micromamba create -y -q -p /mnt/w/env -c conda-forge \
  "pdal>=2.10" python-pdal numpy scipy matplotlib laspy lazrs-python pyproj pillow rasterio \
  scikit-learn scikit-image contourpy
echo "pilejob: env ready $(date -u)"
gcloud storage cp "$W/in/*" .
mkdir -p old && gcloud storage cp $OLD/t678_4376.laz $OLD/t680_4376.laz old/
gcloud storage cp $RAW raw.las
echo "pilejob: inputs ready $(date -u)"
export HOME=/mnt/w MPLCONFIGDIR=/mnt/w/mpl GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif
./env/bin/python extend_tiles.py raw.las old tiles t678_4376 t680_4376 t678_4378 t680_4378
rc=$?
echo "pilejob: tiles rc=$rc $(date -u)"
if [ $rc = 0 ]; then
  MIN_Y=4377141 ID_START=1019 WORKERS=4 ./env/bin/python pile_detect.py train_rows.json piles.json units.json tiles out 2> >(grep -v -E 'Warning|nanmedian' >&2)
  rc=$?
  MIN_Y=4377141 ID_START=1019 ./env/bin/python pile_detect.py --merge piles.json out new_piles.json
  echo "pilejob: detection rc=$rc $(date -u)"
  FETCH_ONLY=1 GROUND_DIR=/mnt/w/ground22 WORKERS=$(nproc) ./env/bin/python pile_views.py new_piles.json tiles views_fetch 2> >(grep -v 'PDAL Error' >&2)
  GROUND_DIR=/mnt/w/ground22 WORKERS=$(nproc) ./env/bin/python pile_views.py new_piles.json tiles views 2> >(grep -v 'PDAL Error' >&2)
  echo "pilejob: views $(ls views/*.webp 2>/dev/null | wc -l) $(date -u)"
  gcloud storage rsync -r tiles $W/tiles
  gcloud storage rsync -r out $W/out
  gcloud storage rsync -r views $W/views
  gcloud storage cp new_piles.json $W/
fi
gcloud storage cp -q /var/log/pilejob.log $W/job.log
echo $rc | gcloud storage cp - $W/DONE && echo "pilejob: DONE written"
sleep 5
poweroff
