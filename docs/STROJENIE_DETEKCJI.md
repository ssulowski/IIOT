# Strojenie detekcji

Profil domyslny jest teraz dobrany pod male ptaki podobne do nagrania testowego oraz troche wieksze obiekty, np. samoloty na dalszym planie.

Gotowy profil jest w:

```text
config/pi_config.birds_airplanes.yaml
```

Najwazniejsze ustawienia:

```yaml
camera:
  fps: 10

recording:
  pre_seconds: 4
  post_seconds: 15
  max_event_seconds: 60

detection:
  process_width: 320
  background_history: 420
  background_var_threshold: 38
  min_area: 4
  max_area: 420
  max_global_motion_ratio: 0.010
  max_candidates_per_frame: 3
  max_candidate_area_ratio: 0.004
  max_bbox_area: 900
  max_aspect_ratio: 5.0
  min_fill_ratio: 0.12
  max_fill_ratio: 0.95
  min_contrast: 10
  min_dark_contrast: 12
  min_track_hits: 3
  track_ttl_frames: 8
  min_track_distance: 7
  min_track_speed: 1.5
  merge_distance: 32

compression:
  crf: 12
  sharpen: true
  delete_raw_after_compress: false
```

Co to robi:

- `min_area: 4` lapie bardzo male ptaki, ktore na przeskalowanej klatce maja tylko kilka pikseli.
- `max_area: 420` zostawia zapas na wiekszego ptaka, drona albo daleki samolot.
- `min_dark_contrast: 12` wymaga, zeby obiekt byl ciemniejszy od otoczenia. To mocno ogranicza falszywe detekcje jasnych krawedzi chmur.
- `min_track_hits: 3` wystarcza na szybkie ptaki widoczne tylko przez kilka klatek.
- `min_track_speed: 1.5` odrzuca wolno przesuwajace sie fragmenty chmur.
- `merge_distance: 32` pozwala sledzic szybki obiekt, ktory robi wiekszy skok miedzy klatkami.
- `max_global_motion_ratio`, `max_candidates_per_frame` i `max_candidate_area_ratio` odrzucaja ruch chmur.
- `fps: 10` jest celowo konserwatywne dla Raspberry Pi Zero 2 W. Przy 15 FPS Pi moze nie nadazac z zapisem i film wyglada wtedy jak przyspieszony.
- `post_seconds: 15` nagrywa dlugo po ostatniej detekcji, zeby ptak mial czas doleciec do brzegu kadru nawet jesli detektor szybko go zgubi.
- `crf: 12` daje bardzo wysoka jakosc MP4, zeby mala czarna kropka nie znikala przez kompresje.
- `delete_raw_after_compress: false` zostawia surowy AVI na RPi do diagnostyki. Gdy wszystko juz dziala, mozna zmienic na `true`.

Jezeli nadal lapie chmury, zaostrz po kolei:

```yaml
max_global_motion_ratio: 0.010
max_candidates_per_frame: 2
max_candidate_area_ratio: 0.0025
background_var_threshold: 45
min_track_hits: 4
min_contrast: 14
min_dark_contrast: 16
min_track_speed: 2.0
```

Jezeli przestanie lapac male ptaki, luzuj po kolei:

```yaml
min_area: 3
min_contrast: 8
min_dark_contrast: 8
min_track_distance: 4
max_candidates_per_frame: 7
```

Jezeli film wyglada jak przyspieszony, zaktualizuj `edge/sky_watcher.py` na RPi. Nowsza wersja zapisuje liczbe klatek i rozciaga MP4 do realnego czasu zdarzenia podczas kompresji.
