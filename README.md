# Sky Watcher IIoT

Projekt do obserwacji nieba z Raspberry Pi Zero 2 W i kamerki. Skrypt na RPi wykrywa małe obiekty latające, nagrywa zdarzenie z buforem sprzed detekcji, kompresuje materiał i wysyła go do serwera HTTP. Serwer zapisuje plik oraz metadane, a później może przekazywać nagrania do klasyfikacji AI.

## Architektura

1. Raspberry Pi uruchamia `edge/sky_watcher.py`.
2. Kamera stale dostarcza klatki w niskiej rozdzielczości.
3. Detektor odrzuca duże/globalne zmiany obrazu, np. ruch chmur, i akceptuje tylko kompaktowe obiekty widoczne przez kilka klatek.
4. Po detekcji nagrywane jest zdarzenie z kilkusekundowym buforem wstecznym.
5. Nagranie jest kompresowane przez `ffmpeg` do MP4/H.264.
6. RPi wysyła plik i metadane do `server/app/main.py`.
7. Serwer zapisuje pliki w `server/data/events`.

## Raspberry Pi

Na Raspberry Pi OS uruchom:

```bash
sudo apt update
sudo apt install -y python3-venv python3-opencv python3-picamera2 ffmpeg
chmod +x scripts/*.sh
./scripts/install_rpi.sh
cp config/pi_config.example.yaml config/pi_config.yaml
```

W `config/pi_config.yaml` ustaw:

- `upload.server_url`, np. adres z Cloudflare Tunnel,
- `upload.api_key`, taki sam jak na serwerze,
- parametry kamery i detekcji, jeśli trzeba je dostroić.

Profil pod małe ptaki i dalekie samoloty jest w `config/pi_config.birds_airplanes.yaml`.
Jeżeli kamera nagrywa ciągle przy ruchu chmur, użyj wskazówek z `docs/STROJENIE_DETEKCJI.md`.

Start ręczny:

```bash
./scripts/run_edge.sh
```

Instalacja jako usługa systemd:

```bash
sudo ./scripts/setup_systemd_pi.sh
sudo systemctl enable --now sky-watcher.service
```

## Serwer

Lokalnie lub na dowolnym tanim/darmowym VPS:

```bash
cd server
cp .env.example .env
docker compose up -d --build
```

Serwer działa na `http://localhost:8080`.

Bez Dockera:

```bash
./scripts/start_server_local.sh
```

Na Windows bez Dockera:

```powershell
.\scripts\start_server_windows.ps1
```

Cloudflare Quick Tunnel do testów:

```bash
./scripts/start_cloudflare_quick_tunnel.sh
```

Cloudflare Quick Tunnel generuje losowy adres `trycloudflare.com` i według dokumentacji Cloudflare jest przeznaczony do testów/dev. Do stabilnego projektu użyj nazwanego Cloudflare Tunnel z tokenem:

```bash
cd server
CLOUDFLARE_TUNNEL_TOKEN="..." docker compose --profile tunnel up -d --build
```

## Test uploadu

```bash
curl -X POST http://localhost:8080/api/v1/events \
  -H "X-API-Key: change-me" \
  -F 'metadata={"event_id":"manual-test","source":"curl"}' \
  -F "video=@sample.mp4"
```

## Dalsza analiza AI

Serwer zapisuje metadane w JSON. W kolejnym kroku można dodać worker, który obserwuje `server/data/events`, pobiera nowe MP4 i wysyła je do klasyfikatora w chmurze. W metadanych jest już pole `analysis_status: "queued"`, żeby łatwo dołożyć kolejkę.
