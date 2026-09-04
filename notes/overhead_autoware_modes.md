# Autoware three-mode overhead — overhead_aw_20260831_133353 (reps: base=3, lttng=3, nsys=3)

| metric | baseline | lttng-only (Δ) | lttng+nsys (Δ) |
|---|--:|--:|--:|
| CPU cores (busiest 60 s) | 3.86±0.03 | 7.46±0.10 (+93.4%) | 6.63±0.01 (+71.8%) |
| objects rate [Hz] | 9.74±0.11 | 9.78±0.06 (+0.4%) | 9.67±0.06 (-0.7%) |
| obstacle pc rate [Hz] | 9.83±0.11 | 9.25±0.48 (-5.9%) | 9.53±0.10 (-3.1%) |
| tsm violations (post-init) | 0.00±0.00 | 0.00±0.00 | 0.00±0.00 |
| concat processing [ms] | 6.44±0.15 | 11.20±0.40 (+73.9%) | 11.35±0.31 (+76.2%) |
| concat pipeline lat [ms] | 182.70±0.12 | 196.08±0.15 (+7.3%) | 197.45±1.15 (+8.1%) |
| centerpoint proc [ms] (GPU) | 22.69±0.13 | 22.82±0.71 (+0.6%) | 23.03±0.11 (+1.5%) |
| Total Latency [ms] | 380.77±6.05 | 410.25±11.04 (+7.7%) | 410.69±10.59 (+7.9%) |
|   multi_object_tracker [ms] | 135.96±6.62 | 159.99±7.88 (+17.7%) | 160.39±6.97 (+18.0%) |
