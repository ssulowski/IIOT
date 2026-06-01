# Instrukcja uruchomienia projektu Sky Watcher IIoT

Stan na 2026-06-01.

## Co wrzucić na Raspberry Pi

Na Raspberry Pi najprościej wrzucić cały katalog projektu, ale realnie potrzebne są te elementy:

- `edge/`
- `config/`
- `scripts/`
- `README.md`

Najwygodniej skopiować cały katalog `IIOT`, bo wtedy ścieżki z dokumentacji będą się zgadzały.

Z Windows PowerShell:

```powershell
cd C:\Users\ssulo\Documents
tar -czf IIOT.tar.gz IIOT
scp .\IIOT.tar.gz pi@raspberrypi.local:~/
```

Na Raspberry Pi:

```bash
cd ~
tar -xzf IIOT.tar.gz
mv IIOT sky-watcher
cd ~/sky-watcher
chmod +x scripts/*.sh
```

Jeżeli `raspberrypi.local` nie działa, sprawdź IP w routerze i użyj np.:

```powershell
scp .\IIOT.tar.gz pi@192.168.1.50:~/
```

## Konfiguracja Raspberry Pi

Zalecany system: Raspberry Pi OS 64-bit albo aktualny Raspberry Pi OS Lite. Kamera powinna działać przez `libcamera`/`picamera2`.

Sprawdź kamerę:

```bash
libcamera-hello
```

Zainstaluj zależności:

```bash
cd ~/sky-watcher
sudo apt update
sudo apt install -y python3-venv python3-opencv python3-picamera2 ffmpeg curl
./scripts/install_rpi.sh
cp config/pi_config.example.yaml config/pi_config.yaml
```

Edytuj konfigurację:

```bash
nano config/pi_config.yaml
```

Najważniejsze pola:

```yaml
camera:
  backend: auto
  width: 640
  height: 480
  fps: 12

upload:
  enabled: true
  server_url: "https://TWOJ-ADRES.trycloudflare.com"
  api_key: "TEN-SAM-KLUCZ-CO-NA-SERWERZE"
```

Ważne: `server_url` ma być samym adresem bazowym, bez `/api/v1/events`. Skrypt dopisuje końcówkę sam.

Start ręczny:

```bash
./scripts/run_edge.sh --log-level INFO
```

Start jako usługa po włączeniu RPi:

```bash
sudo ./scripts/setup_systemd_pi.sh
sudo systemctl enable --now sky-watcher.service
```

Logi:

```bash
journalctl -u sky-watcher -f
```

Restart po zmianie konfiguracji:

```bash
sudo systemctl restart sky-watcher.service
```

## Lokalny serwer na komputerze

### Wariant A: Docker Desktop na Windows

W PowerShell:

```powershell
cd C:\Users\ssulo\Documents\IIOT\server
copy .env.example .env
notepad .env
```

Ustaw np.:

```env
API_KEY=tu-wpisz-dlugi-losowy-klucz
STORAGE_DIR=/data
MAX_UPLOAD_MB=90
```

Uruchom:

```powershell
docker compose up -d --build
docker compose ps
curl http://localhost:8080/health
```

Pliki będą zapisywane w:

```text
C:\Users\ssulo\Documents\IIOT\server\data\events
```

Zatrzymanie:

```powershell
docker compose down
```

### Wariant B: bez Dockera na Linux/WSL/RPi/VPS

```bash
cd ~/sky-watcher
./scripts/start_server_local.sh
```

## Wystawienie lokalnego serwera przez Cloudflare Quick Tunnel

Quick Tunnel jest dobry do testów i pokazu projektu, ale adres zmienia się po restarcie tunelu.

Na Windows zainstaluj `cloudflared`:

```powershell
winget install --id Cloudflare.cloudflared
```

Najpierw uruchom serwer:

```powershell
cd C:\Users\ssulo\Documents\IIOT\server
docker compose up -d --build
```

Potem uruchom tunel:

```powershell
cloudflared tunnel --url http://localhost:8080
```

W terminalu pojawi się adres typu:

```text
https://nazwa-losowa.trycloudflare.com
```

Ten adres wpisujesz na Raspberry Pi w:

```yaml
upload:
  server_url: "https://nazwa-losowa.trycloudflare.com"
  api_key: "TEN-SAM-KLUCZ-CO-W-SERVER/.env"
```

Potem restart RPi:

```bash
sudo systemctl restart sky-watcher.service
```

Uwaga: Cloudflare Free/Pro ma limit uploadu 100 MB na request. Dlatego w konfiguracji serwera i RPi trzymaj nagrania krótkie i skompresowane. W `.env` ustaw `MAX_UPLOAD_MB=90`, a w `pi_config.yaml` zostaw krótkie `post_seconds` i rozsądny `crf`, np. `28-32`.

## Test połączenia RPi -> serwer

Na Raspberry Pi:

```bash
SERVER_URL="https://nazwa-losowa.trycloudflare.com"
API_KEY="TEN-SAM-KLUCZ"

curl "$SERVER_URL/health"
ffmpeg -y -f lavfi -i testsrc=duration=2:size=320x240:rate=12 -c:v libx264 /tmp/test.mp4
curl -X POST "$SERVER_URL/api/v1/events" \
  -H "X-API-Key: $API_KEY" \
  -F 'metadata={"event_id":"test-rpi","source":"manual"}' \
  -F "video=@/tmp/test.mp4"
curl -H "X-API-Key: $API_KEY" "$SERVER_URL/api/v1/events"
```

Jeżeli ostatnia komenda pokazuje event `test-rpi`, upload działa.

## VPS: co wybrać

Najlepsze opcje do tego projektu:

1. Oracle Cloud Always Free - najlepsze, jeśli ma być naprawdę za darmo. Daje bardzo dużo zasobów jak na darmowy VPS, ale czasem brakuje dostępności w regionie i Oracle może odzyskać zasoby, jeśli instancja jest długo bezczynna.
2. Hetzner Cloud CX23 albo CAX11 w Niemczech/Finlandii - najlepszy tani płatny wybór w Europie. Wystarczy do FastAPI, Dockera i przechowywania małej liczby klipów.
3. DigitalOcean Droplet - najprostszy panel i dokumentacja, ale zwykle gorszy stosunek ceny do zasobów niż Hetzner.

Moja rekomendacja:

- Do projektu na studia i kosztu zero: Oracle Cloud Always Free, Ubuntu 24.04, Ampere A1, 1-2 OCPU, 6-12 GB RAM, 80-150 GB dysku.
- Jeśli Oracle nie pozwala utworzyć instancji: Hetzner CX23, region Nuremberg/Falkenstein/Helsinki, Ubuntu 24.04.

## Instalacja serwera na VPS

Na VPS wybierz Ubuntu 24.04 LTS. Po utworzeniu maszyny skopiuj projekt.

Z Windows PowerShell:

```powershell
cd C:\Users\ssulo\Documents
tar -czf IIOT.tar.gz IIOT
scp .\IIOT.tar.gz root@ADRES_IP_VPS:/opt/
```

Na VPS:

```bash
cd /opt
tar -xzf IIOT.tar.gz
mv IIOT sky-watcher
cd /opt/sky-watcher
chmod +x scripts/*.sh
sudo ./scripts/setup_server_ubuntu.sh
```

Skrypt:

- instaluje Dockera,
- tworzy `server/.env`,
- generuje `API_KEY`,
- uruchamia serwer na porcie `8080`.

Sprawdzenie:

```bash
cd /opt/sky-watcher/server
cat .env
docker compose ps
curl http://localhost:8080/health
```

Jeżeli chcesz testowo wysyłać bez Cloudflare, ustaw na RPi:

```yaml
upload:
  server_url: "http://ADRES_IP_VPS:8080"
  api_key: "KLUCZ-Z-SERVER/.env"
```

Wtedy w firewallu VPS musisz dopuścić port `8080`. Bezpieczniej jest użyć Cloudflare Tunnel i nie wystawiać portu 8080 publicznie.

## Stabilny Cloudflare Tunnel na VPS

Do stabilnego adresu najlepiej mieć własną domenę podpiętą do Cloudflare. Darmowy plan wystarczy.

W panelu Cloudflare:

1. Wejdź w Zero Trust.
2. Networks -> Tunnels.
3. Create tunnel.
4. Wybierz `cloudflared`.
5. Nadaj nazwę, np. `sky-watcher`.
6. Skopiuj token tunelu.
7. Dodaj public hostname, np. `sky.twojadomena.pl`.
8. Jeżeli używasz `cloudflared` z tego projektu w Docker Compose, jako Service URL wpisz:

```text
http://sky-server:8080
```

Na VPS:

```bash
cd /opt/sky-watcher/server
nano .env
```

Dopisz:

```env
CLOUDFLARE_TUNNEL_TOKEN=WKLEJONY_TOKEN
```

Uruchom tunel:

```bash
docker compose --profile tunnel up -d
docker compose ps
```

Na RPi ustaw:

```yaml
upload:
  server_url: "https://sky.twojadomena.pl"
  api_key: "KLUCZ-Z-SERVER/.env"
```

Restart:

```bash
sudo systemctl restart sky-watcher.service
```

## Gdzie trafiają nagrania

Na Raspberry Pi, zanim upload się powiedzie:

```text
~/sky-watcher/recordings/unsent
```

Po udanym uploadzie na RPi:

```text
~/sky-watcher/recordings/unsent/uploaded
```

Na serwerze:

```text
server/data/events/RRRR/MM/DD
```

Przykład listowania eventów:

```bash
curl -H "X-API-Key: KLUCZ" https://ADRES_SERWERA/api/v1/events
```

## Szybka kolejność dla prezentacji

1. Uruchom serwer lokalnie Dockerem.
2. Uruchom `cloudflared tunnel --url http://localhost:8080`.
3. Skopiuj URL `trycloudflare.com`.
4. Wpisz URL i API key w `config/pi_config.yaml` na Raspberry Pi.
5. Uruchom `./scripts/run_edge.sh` albo usługę systemd.
6. Zrób test uploadu komendą `curl`.
7. Potem zostaw RPi skierowane w niebo i sprawdź `server/data/events`.

