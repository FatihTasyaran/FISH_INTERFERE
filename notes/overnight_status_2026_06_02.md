# FISH Isaac sweep — overnight status (2026-06-02 morning)

## Tamamlanan (14/24 benchmark, fish_graph.json + fish_viz.html üretildi)

| # | Benchmark | V | E | GPU-F | nsys files |
|---|---|---:|---:|---:|---:|
| 1 | apriltag | 86 | 104 | 26 | 1 |
| 2 | bi3d | 178 | 235 | 58 | 1 |
| 3 | bi3d_freespace | 173 | 246 | 57 | 1 |
| 4 | dnn_image_encoder | 174 | 263 | 66 | 1 |
| 5 | dope | 206 | 317 | 74 | 1 |
| 6 | ess | 100 | 113 | 31 | 1 |
| 7 | image_proc | 82 | 108 | 30 | 1 |
| 8 | nitros_bridge | 101 | 125 | 32 | 1 |
| 9 | nvblox | 199 | 242 | 59 | 1 |
| 10 | pynitros | 137 | 177 | 21 | 3 |
| 11 | stereo_image_proc | 148 | 169 | 46 | 1 |
| 12 | tensor_rt | 151 | 211 | 53 | 1 |
| 13 | triton | 143 | 190 | 47 | 1 |
| 14 | unet | 170 | 248 | 64 | 1 |

`fish_graph.json` ve `fish_viz.html` her benchmark için `/tmp/fish_traces_v3/<bench>/measure/` altında.

## Kalan (6 benchmark — fix'leri uygulandı ama sweep'i 9 sa uyumadığı için bitmedi)

Hepsi için sweep_v3.sh ve manual_models'a fix uygulandı — TEK YAPILACAK:
sweep script'i yeniden çalıştırmak.

| Benchmark | Eksik kalan / fix uygulandı mı? |
|---|---|
| detectnet | libdcgm + TAO peoplenet model — fix VAR, sweep gerek |
| centerpose | libdcgm — fix VAR, sweep gerek |
| segformer | peoplesemsegformer.onnx — fix VAR, sweep gerek |
| foundationpose | r2b_robotarm bag (2024 dataset) — fix VAR, sweep gerek |
| rtdetr | r2b_robotarm bag — fix VAR, sweep gerek |
| segment_anything | r2b_robotarm bag — fix VAR, sweep gerek |

## Atlanan (2)

- `visual_slam` — cleanup hang (önceki v3 notlarında belirtilmişti)
- `occupancy_grid_localizer` — hiç denenmedi (toplam 22'yi hedeflediğimiz için)

## Bu gece çözülen sorunlar (sweep_v3.sh + manual_models'a baked)

1. **apt-get permission-denied** → version-pinned dpkg ile `libnvinfer-bin` install
2. **heredoc escape** → `\$NVINFER_VER` (outer shell expansion'ı durdur)
3. **`ls -d` vs `ls -td`** → postprocess yanlış session seçiyordu (en eskisi)
4. **`fish_viz.html` accidentally selected as session** → `ls -td -- .../fish_*/` (slash + filter)
5. **Watchdog container-clock-skew** → docker container'lar ~10sa geride çalışıyor, `date +%s` host'tan değil container'dan alınmalı (uygulandı)

## Yeni indirilen modeller/dosyalar (`/home/tue037807/isaac_ros_assets/`)

```
manual_models/
├── bi3d/                          (önceden vardı)
├── centerpose_shoe/               (önceden vardı)
├── ess/
│   ├── ess.onnx                   (NGC nvidia/isaac/dnn_stereo_disparity)
│   ├── light_ess.onnx
│   └── plugins/x86_64/ess_plugins.so   ← önemli, trtexec için
├── foundationpose/                (önceden vardı)
├── ketchup/                       (DOPE, önceden vardı)
├── peoplenet/
│   ├── resnet34_peoplenet.onnx    (NGC nvidia/tao/peoplenet:deployable_quantized_onnx_v2.6.3)
│   ├── resnet34_peoplenet_int8.txt
│   ├── labels.txt
│   └── config.pbtxt
├── peoplesemsegformer/
│   └── peoplesemsegformer.onnx    (NGC nvidia/tao/peoplesemsegformer:deployable_v1.0, 204 MB)
├── peoplesemsegnet_shuffleseg/
│   ├── peoplesemsegnet_shuffleseg.onnx   (NGC nvidia/isaac/optimized-peoplesemseg-amr:v1.0)
│   └── config.pbtxt
├── sdetr/                         (önceden vardı)
└── segment_anything/              (önceden vardı)

apt_cache/
└── datacenter-gpu-manager_3.3.9_amd64.deb   (911 MB, dcgm runtime)
```

Plus: `/home/tue037807/r2bdataset2024_v1/r2b_robotarm/` (user'ın indirdiği bag).

## Sabah uyandıktan sonra yapılacak (10:30 itibariyle)

```bash
# 1. Sweep'i yeniden başlat (6 kalan benchmark, ~50 dk):
cd /home/tue037807/fish_interfere
nohup bash /tmp/sweep_watchdog.sh > /tmp/watchdog.log 2>&1 &
SMOKE_TIMEOUT=900 bash scripts/overnight_sweep_v3.sh \
    isaac_ros_detectnet isaac_ros_centerpose isaac_ros_segformer \
    isaac_ros_foundationpose isaac_ros_rtdetr isaac_ros_segment_anything \
    > /tmp/sweep_v3_final.log 2>&1 &

# 2. Bitince postprocess + container_node:
#    detectnet=container, centerpose=centerpose_container,
#    segformer=segformer_container, foundationpose=container,
#    rtdetr=container, segment_anything=sam_container
declare -A CN
CN[isaac_ros_detectnet]=container
CN[isaac_ros_centerpose]=centerpose_container
CN[isaac_ros_segformer]=segformer_container
CN[isaac_ros_foundationpose]=container
CN[isaac_ros_rtdetr]=container
CN[isaac_ros_segment_anything]=sam_container
for b in "${!CN[@]}"; do
    bash /tmp/sweep_postprocess.sh "$b" "${CN[$b]}"
done
```

## Bugünün morali

Bu gece 8 yeni benchmark eklendi (14 toplam başarı). Geriye 6 var — hepsinin
fix'i hazır, sadece tekrar çalıştırılması gerek. Eğer 02:00'de süreç ölmeseydi
hepsi bitmişti.
