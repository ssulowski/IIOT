# Strojenie detekcji

Aktualny profil jest mocno anty-chmurowy i dobrany pod ciemne sylwetki: male ptaki, dalekie samoloty i drony widoczne jako czarny punkt na jasniejszym tle.

Gotowy profil:

```text
config/pi_config.birds_airplanes.yaml
```

Najwazniejsze ustawienia:

```yaml
camera:
  width: 640
  height: 480
  fps: 8
  ae_enable: true
  exposure_time_us: 0
  analogue_gain: 0.0
  awb_enable: true

recording:
  pre_seconds: 4
  post_seconds: 15
  max_event_seconds: 60
  raw_fourcc: MJPG
  raw_quality: 95
  writer_queue_frames: 64

detection:
  process_width: 480
  background_history: 500
  background_var_threshold: 45
  min_area: 4
  max_area: 180
  max_global_motion_ratio: 0.006
  max_candidates_per_frame: 2
  max_candidate_area_ratio: 0.0025
  max_bbox_area: 260
  max_aspect_ratio: 3.5
  min_contrast: 14
  min_dark_contrast: 18
  max_foreground_brightness: 115
  min_surround_brightness: 80
  max_surround_stddev: 42
  min_track_hits: 4
  min_track_distance: 8
  min_track_speed: 1.7
  merge_distance: 28

compression:
  crf: 24
  scale_width: 640
  sharpen: true
  delete_raw_after_compress: true
```

Co jest najwazniejsze:

- `min_dark_contrast` wymaga, zeby obiekt byl ciemniejszy od otoczenia. To odcina jasne krawedzie chmur.
- `max_foreground_brightness` odrzuca kandydaty, ktore sa zbyt jasne jak na ptaka/samolot jako sylwetke.
- `max_surround_stddev` odrzuca mocno teksturalne fragmenty chmur. Ptak na gladkim niebie ma zwykle spokojniejsze otoczenie.
- `max_global_motion_ratio`, `max_candidates_per_frame` i `max_candidate_area_ratio` odrzucaja ruch duzych chmur.
- `min_track_speed` odrzuca wolno przesuwajace sie fragmenty chmur.
- `640x480` jest stabilnym natywnym trybem OV5647. `process_width: 480` daje detektorowi wiecej szczegolow niz 320 bez podbijania zapisu wideo.
- `crf: 24` daje mniejsze pliki MP4. Skoro znikanie ptaka bylo widoczne tez w AVI, nie ma sensu trzymac bardzo wysokiego bitrate.
- `delete_raw_after_compress: true` usuwa surowy AVI po kompresji, zeby nie zapychac karty SD.
- Domyslnie kamera uzywa autoekspozycji (`ae_enable: true`). Jezeli jasnosc kadru plywa przez chmury, testowo wlacz manual:

```yaml
camera:
  ae_enable: false
  exposure_time_us: 2500
  analogue_gain: 1.0
  awb_enable: true
```

W dzien przy jasnym niebie zwykle testuj `exposure_time_us` w zakresie `1000-5000`. Za ciemno: zwieksz exposure albo gain. Za jasno/przepalone chmury: zmniejsz exposure.

Jezeli nadal lapie chmury, zaostrz:

```yaml
max_global_motion_ratio: 0.004
max_candidates_per_frame: 1
max_candidate_area_ratio: 0.0015
min_dark_contrast: 24
max_foreground_brightness: 90
max_surround_stddev: 30
min_track_hits: 5
min_track_speed: 2.2
```

Jezeli przestaje lapac ptaki, luzuj pojedynczo:

```yaml
min_dark_contrast: 12
max_foreground_brightness: 140
max_surround_stddev: 55
min_track_hits: 3
max_area: 260
max_bbox_area: 420
```

Jezeli ptak znika w AVI, to nie jest problem uploadu ani MP4. Oznacza to, ze kamera zapisuje go tylko przez kilka klatek: obiekt ma za malo pikseli, traci kontrast albo zlewa sie z tlem. Wtedy najbardziej pomaga skierowanie kamery tak, zeby niebo zajmowalo wiecej kadru, zoom/optyka o wezszym kacie albo wyzsza rozdzielczosc przy nizszym FPS.
