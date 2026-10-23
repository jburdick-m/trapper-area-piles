#!/bin/bash
# GCE startup script: render every pile view from bucket data, upload, power off.
#   gcloud compute instances create pile-views-22core --zone us-west1-a --machine-type c3-standard-22 \
#     --image-family debian-12 --image-project debian-cloud --boot-disk-size 50GB --scopes cloud-platform \
#     --max-run-duration 2h --instance-termination-action DELETE \
#     --metadata-from-file startup-script=tools/pile_views_vm.sh
# Inputs:  $W/in/pile_views.py, $W/in/piles.json, $W/ground22/ (2022 ground per pile,
#          from FETCH_ONLY=1 GROUND_DIR=... pile_views.py), the Feb 2026 view tiles in $TILES
# Outputs: $W/out/pile_<id>.webp, $W/out/summary.json, $W/job.log, $W/DONE (python exit code)
W=gs://aso-august-w1/pc_work_10212026/pile_views
TILES=gs://aso-august-w1/pc_work_10212026/pike_view2/tiles
exec > >(tee /var/log/pilejob.log > /dev/ttyS0) 2>&1
(while true; do gcloud storage cp -q /var/log/pilejob.log $W/job.log || echo "pilejob: log upload failed"; sleep 30; done) &
echo "pilejob: start $(date -u)"
mkdir -p /mnt/w && cd /mnt/w
apt-get update -qq && apt-get install -y -qq bzip2
curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj bin/micromamba
rm -rf /mnt/w/env out
MAMBA_ROOT_PREFIX=/mnt/w/mamba ./bin/micromamba create -y -q -p /mnt/w/env -c conda-forge \
  "pdal>=2.10" python-pdal numpy scipy matplotlib laspy lazrs-python pyproj pillow
echo "pilejob: env ready $(date -u) $(./env/bin/pdal --version 2>/dev/null | grep -o "pdal [0-9.]*")"
gcloud storage cp $W/in/pile_views.py $W/in/piles.json .
gcloud storage rsync -r $W/ground22 ground22
gcloud storage rsync -r $TILES tiles
echo "pilejob: inputs ready $(date -u): $(ls ground22 | wc -l) ground files, $(ls tiles | wc -l) tiles"
# startup scripts run without HOME, which matplotlib needs for its config dir
HOME=/mnt/w MPLCONFIGDIR=/mnt/w/mpl GROUND_DIR=/mnt/w/ground22 WORKERS=$(nproc) \
  ./env/bin/python pile_views.py piles.json tiles out 2> >(grep -v 'PDAL Error' >&2)
rc=$?
echo "pilejob: python rc=$rc $(date -u): $(ls out/*.webp 2>/dev/null | wc -l) images"
gcloud storage rsync -r out $W/out && echo "pilejob: outputs uploaded"
gcloud storage cp -q /var/log/pilejob.log $W/job.log
echo $rc | gcloud storage cp - $W/DONE && echo "pilejob: DONE written"
sleep 5
poweroff
