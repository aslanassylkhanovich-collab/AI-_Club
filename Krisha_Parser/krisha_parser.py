import csv
import json
import math
import os
import random
import re
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


BASE = "https://krisha.kz"

# город -> как он называется в адресе krisha.kz (проверено)
CITIES = {
    "Алматы": "almaty",
    "Астана": "astana",
    "Шымкент": "shymkent",
    "Актобе": "aktobe",
    "Караганда": "karaganda",
    "Тараз": "taraz",
    "Атырау": "atyrau",
    "Усть-Каменогорск": "ust-kamenogorsk",
    "Павлодар": "pavlodar",
    "Кызылорда": "kyzylorda",
    "Семей": "semej",
    "Костанай": "kostanaj",
    "Кокшетау": "kokshetau",
    "Актау": "aktau",
    "Уральск": "uralsk",
    "Петропавловск": "petropavlovsk",
    "Туркестан": "turkestan",
    "Талдыкорган": "taldykorgan",
    "Экибастуз": "ekibastuz",
}


@dataclass
class FlatRow:
    name: Optional[str] = None
    district: Optional[str] = None
    address: Optional[str] = None
    price: Optional[int] = None
    owner: Optional[str] = None
    complex_name: Optional[str] = None
    house_type: Optional[str] = None
    construction_year: Optional[int] = None
    ceiling_height: Optional[str] = None
    bathroom_info: Optional[str] = None
    area: Optional[float] = None
    room_count: Optional[int] = None
    floor: Optional[int] = None
    floor_count: Optional[int] = None
    url: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    information: Optional[str] = None


@dataclass
class ListingRow:
    city: str = ""
    name: Optional[str] = None
    district: Optional[str] = None
    address: Optional[str] = None
    price: Optional[int] = None
    price_text: Optional[str] = None
    price_is_from: bool = False
    owner: Optional[str] = None
    complex_name: Optional[str] = None
    house_type: Optional[str] = None
    construction_year: Optional[int] = None
    ceiling_height: Optional[str] = None
    bathroom_info: Optional[str] = None
    area: Optional[float] = None
    room_count: Optional[int] = None
    floor: Optional[int] = None
    floor_count: Optional[int] = None
    url: Optional[str] = None
    information_preview: Optional[str] = None


class AccessBlocked(RuntimeError):
    """Откладываем URL; stop_run требует остановить весь сбор."""

    def __init__(self, message: str, stop_run: bool = False):
        super().__init__(message)
        self.stop_run = stop_run


@dataclass(frozen=True)
class DelayPolicy:
    """Границы адаптации; это настройки, а не известные лимиты сайта."""

    initial_delay: float = 10.0
    min_delay: float = 3.0
    max_delay: float = 60.0
    success_window: int = 5

    def __post_init__(self):
        delays = (self.min_delay, self.initial_delay, self.max_delay)
        if (not all(math.isfinite(v) for v in delays)
                or not 0 < self.min_delay <= self.initial_delay <= self.max_delay
                or not isinstance(self.success_window, int) or self.success_window < 1):
            raise ValueError("Нужны 0 < min_delay <= initial_delay <= max_delay и success_window >= 1")


class KrishaSession(requests.Session):
    def __init__(
        self, delay_range: Optional[Tuple[float, float]] = None,
        delay_policy: Optional[DelayPolicy] = None,
    ):
        super().__init__()
        if delay_range is not None and delay_policy is not None:
            raise ValueError("Выберите delay_range или delay_policy")
        if delay_range is not None and (not all(math.isfinite(v) for v in delay_range)
                                       or not 0 <= delay_range[0] <= delay_range[1]):
            raise ValueError("Нужны паузы 0 <= минимум <= максимум")
        self.delay_range = delay_range
        self.delay_policy = delay_policy or DelayPolicy()
        self.current_delay = delay_range[1] if delay_range is not None else self.delay_policy.initial_delay
        self.success_streak = 0
        self.next_request_at = 0.0
        self.block_retries_used = 0

    def slow_down(self):
        self.success_streak = 0
        if self.delay_range is None:
            self.current_delay = min(self.delay_policy.max_delay, self.current_delay * 2)

    def record_success(self):
        if self.delay_range is None:
            self.success_streak += 1
            if self.success_streak >= self.delay_policy.success_window:
                self.current_delay = max(self.delay_policy.min_delay, self.current_delay * 0.8)
                self.success_streak = 0

    def request(self, *args, **kwargs):
        time.sleep(max(0.0, self.next_request_at - time.monotonic()))
        server_wait = 0.0
        try:
            response = super().request(*args, **kwargs)
            server_wait = retry_after_seconds(response)
            if (response.status_code in (403, 429, 468) or response.status_code >= 500
                    or "/.safeline/" in response.text.lower() or server_wait > 0):
                self.slow_down()
            elif 200 <= response.status_code < 300:
                self.record_success()
            else:
                self.success_streak = 0
            return response
        except requests.RequestException:
            self.slow_down()
            raise
        finally:
            # Пауза после каждого запроса: выдача, объявление, ошибка, повтор.
            delay = random.uniform(*self.delay_range) if self.delay_range is not None else self.current_delay
            # Retry-After — нижняя граница ожидания, даже если она больше max_delay.
            self.next_request_at = time.monotonic() + max(delay, server_wait)


def make_session(
    delay_range: Optional[Tuple[float, float]] = None,
    delay_policy: Optional[DelayPolicy] = None,
) -> requests.Session:
    s = KrishaSession(delay_range, delay_policy)
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
        }
    )
    return s


def retry_after_seconds(response) -> float:
    value = response.headers.get("Retry-After", "")
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def fetch_html(
    session: requests.Session, url: str, retries: int = 3,
    block_pause: float = 60.0,
    retry_blocked: bool = False,
) -> str:
    if retries < 1 or block_pause < 0:
        raise ValueError("retries >= 1, block_pause >= 0")
    last_err = None
    for attempt in range(1, retries + 1):
        retry_wait = 0.0
        try:
            r = session.get(url, timeout=25)
        except requests.RequestException as e:
            last_err = e
        else:
            blocked = r.status_code in (403, 429, 468) or "/.safeline/" in r.text.lower()
            if blocked:
                retry_after = retry_after_seconds(r)
                # По умолчанию откладываем URL без повтора. При явном retry_blocked
                # разрешён один повтор за сессию. Долгий Retry-After останавливает сбор.
                if (not retry_blocked or getattr(session, "block_retries_used", 0) >= 1
                        or attempt == retries or retry_after > block_pause):
                    suffix = f"; Retry-After: {retry_after:.0f} с" if retry_after else ""
                    raise AccessBlocked(
                        f"HTTP {r.status_code}, проверка/ограничение доступа{suffix}: {url}",
                        stop_run=retry_after > block_pause,
                    )
                session.block_retries_used = 1
                time.sleep(block_pause)
                continue
            if r.status_code not in (500, 502, 503, 504):
                r.raise_for_status()
                return r.text
            last_err = RuntimeError(f"HTTP {r.status_code}: {url}")
            if retry_after_seconds(r) > block_pause:
                raise AccessBlocked(f"HTTP {r.status_code}: сервер просит отложить сбор: {url}", stop_run=True)
            retry_wait = retry_after_seconds(r)
        if attempt < retries:
            time.sleep(max(retry_wait, getattr(session, "current_delay", 10.0 * attempt)))
    raise RuntimeError(f"Не удалось загрузить {url}. Последняя ошибка: {last_err}") from last_err


def normalize_ad_url(href: str) -> str:
    # href может быть "/a/show/100..." или "//krisha.kz/a/show/100..."
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        href = BASE + href
    # параметры после ? не обязательны для парсинга — убираем для стабильности
    return href.split("?")[0]


def extract_listing_urls(list_html: str) -> List[str]:
    soup = BeautifulSoup(list_html, "html.parser")

    urls: List[str] = []
    seen = set()

    # 1) Сначала пробуем самый правильный путь:
    #    ссылки в заголовках карточек центральной ленты
    candidates = soup.select('a.a-card__title[href^="/a/show/"]')

    # 2) Фильтр: выкидываем всё, что находится в <aside> (это почти всегда правая колонка)
    def is_in_aside(tag) -> bool:
        return tag.find_parent("aside") is not None

    for a in candidates:
        if is_in_aside(a):
            continue

        href = a.get("href")
        if not href:
            continue

        u = normalize_ad_url(href)
        if u not in seen:
            seen.add(u)
            urls.append(u)

    # 3) Если вдруг верстка поменялась и a-card__title не найден:
    #    fallback — как раньше, но всё равно отсекаем <aside>
    if not urls:
        for a in soup.find_all("a", href=True):
            if is_in_aside(a):
                continue
            href = a["href"]
            if re.search(r"^/a/show/\d+", href):
                u = normalize_ad_url(href)
                if u not in seen:
                    seen.add(u)
                    urls.append(u)

    return urls


def extract_window_data(page_html: str) -> Dict:
    # Ищем: window.data = {...};
    m = re.search(r"window\.data\s*=\s*(\{.*?\})\s*;\s*</script>", page_html, flags=re.S)
    if not m:
        # fallback: иногда </script> может не попасть в regex
        m = re.search(r"window\.data\s*=\s*(\{.*\})\s*;", page_html, flags=re.S)
    if not m:
        raise ValueError("Не найден window.data в исходнике страницы объявления")

    raw = m.group(1)
    return json.loads(raw)


def parse_floor_text(text: str) -> Tuple[Optional[int], Optional[int]]:
    # "7 из 22", "7/22" или отдельный этаж "7".
    m = re.search(r"(\d+)\s*(?:из|/)\s*(\d+)", text, flags=re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"\s*(\d+)\s*(?:этаж)?\s*", text, flags=re.I)
    return (int(m.group(1)), None) if m else (None, None)


def clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def parse_advert_page(page_html: str, url: str) -> FlatRow:
    data = extract_window_data(page_html)
    advert = data.get("advert", {}) or {}
    adverts0 = (data.get("adverts", []) or [{}])[0] or {}

    soup = BeautifulSoup(page_html, "html.parser")

    # Блоки вида <div class="offer__info-item" data-name="..."> ... <div class="offer__advert-short-info">...</div>
    def get_info_item(data_name: str) -> Optional[str]:
        el = soup.select_one(f'div.offer__info-item[data-name="{data_name}"] .offer__advert-short-info')
        return clean_text(el.get_text(" ", strip=True)) if el else None

    # Параметры (dl dt/dd)
    params: Dict[str, str] = {}
    for dl in soup.select("div.offer__parameters dl"):
        dt = dl.find("dt")
        dd = dl.find("dd")
        if not dt or not dd:
            continue
        key = (dt.get("data-name") or clean_text(dt.get_text()))
        val = clean_text(dd.get_text(" ", strip=True))
        params[key] = val

    # Описание
    descr_el = soup.select_one("div.js-description")
    information = clean_text(descr_el.get_text("\n", strip=True)) if descr_el else None

    # Район (человеческий)
    district_text = None
    loc = soup.select_one("div.offer__location span")
    if loc:
        district_text = clean_text(loc.get_text(" ", strip=True))

    # floor/floor_count
    floor_txt = get_info_item("flat.floor") or ""
    floor, floor_count = parse_floor_text(floor_txt)

    # В характеристиках бывает только "15", а в заголовке — "15/16 этаж".
    heading = soup.select_one("h1")
    heading_text = clean_text(heading.get_text(" ", strip=True)) if heading else ""
    floor_match = re.search(
        r"(?<![\d.,])(\d+\s*(?:(?:/|из)\s*\d+)?\s*этаж)\b",
        heading_text,
        flags=re.I,
    )
    if floor_match:
        title_floor, title_count = parse_floor_text(floor_match.group(1))
        if floor is None:
            floor = title_floor
        # Не смешиваем этаж и этажность из противоречащих друг другу блоков.
        if floor_count is None and floor == title_floor:
            floor_count = title_count

    # address
    address = adverts0.get("fullAddress") or advert.get("addressTitle") or adverts0.get("address")

    # owner
    owner = advert.get("ownerName")
    if not owner:
        owner_obj = adverts0.get("owner") or {}
        owner = owner_obj.get("title")

    # area может быть float/int
    area = advert.get("square")
    try:
        area = float(area) if area is not None else None
    except Exception:
        area = None

    # lat/lon
    lat = (advert.get("map") or {}).get("lat")
    lon = (advert.get("map") or {}).get("lon")

    # construction year из HTML (надежнее, чем из description)
    construction_year = None
    year_txt = get_info_item("house.year")
    if year_txt and re.search(r"\d{4}", year_txt):
        construction_year = int(re.search(r"\d{4}", year_txt).group(0))

    row = FlatRow(
        name=advert.get("title") or adverts0.get("title"),
        district=district_text or (advert.get("address") or {}).get("district"),
        address=address,
        price=advert.get("price"),
        owner=owner,
        complex_name=(get_info_item("map.complex") or None),
        house_type=(get_info_item("flat.building") or None),
        construction_year=construction_year,
        ceiling_height=(
            get_info_item("ceiling")
            or params.get("ceiling")
            or params.get("Высота потолков")
        ),
        bathroom_info=get_info_item("flat.toilet"),
        area=area,
        room_count=advert.get("rooms"),
        floor=floor,
        floor_count=floor_count,
        lat=lat,
        lon=lon,
        url=url,
        information=information or adverts0.get("description"),
    )
    return row


def append_rows_to_csv(csv_path: str, rows: List[FlatRow]) -> None:
    file_exists = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    if not rows and file_exists:
        return
    fieldnames = [field.name for field in fields(FlatRow)]

    with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))


def load_seen_urls(csv_path: str) -> set:
    # url объявлений, которые уже есть в CSV (для продолжения и защиты от дублей)
    if not os.path.exists(csv_path):
        return set()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return {row["url"] for row in csv.DictReader(f) if row.get("url")}


def load_pending(path: str) -> Dict[str, str]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    pending = {}
    for item in entries:
        url = item["url"]
        if not re.fullmatch(r"https://krisha\.kz/a/show/\d+", url):
            raise ValueError(f"Некорректный URL в очереди {path}")
        pending[url] = item.get("last_error", "")
    return pending


def save_pending(path: str, pending: Dict[str, str]) -> None:
    # Замена целого файла: прерванная запись не повреждает старую очередь.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False,
            dir=os.path.dirname(os.path.abspath(path)), prefix=".krisha_pending_",
        ) as f:
            tmp_path = f.name
            json.dump([
                {"url": url, "last_error": reason} for url, reason in pending.items()
            ], f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def parse_krisha(
    city: str,
    start_page: int = 1,
    end_page: int = 3000,
    csv_path: Optional[str] = None,
    save_every: int = 10,
    max_empty_pages: int = 5,
    delay_range: Optional[Tuple[float, float]] = None,
    block_pause: float = 60.0,
    max_ads: Optional[int] = None,
    verbose: bool = False,
    delay_policy: Optional[DelayPolicy] = None,
    max_consecutive_blocks: int = 3,
) -> str:
    """Полные объявления с адаптивной паузой; max_ads — число новых строк за запуск.

    Не собранные URL хранятся в <csv_path>.pending.json. Следующий запуск сначала
    обрабатывает эту очередь; каждый URL запрашивается не более одного раза за
    запуск, кроме повторов сетевых ошибок и временных HTTP 5xx.
    Единичный блок откладывает объявление. Серия из max_consecutive_blocks блоков,
    блокировка выдачи или Retry-After > block_pause останавливает сбор.
    delay_range=(60, 60) включает прежний фиксированный интервал.
    По умолчанию печатается только итог; verbose=True — прогресс по страницам.
    """
    slug = CITIES.get(city, city)
    if slug not in CITIES.values():
        raise ValueError(f"Неизвестный город {city!r}. Доступны: {', '.join(CITIES)}")
    if (start_page < 1 or end_page < start_page or save_every < 1
            or max_empty_pages < 1 or block_pause < 0 or max_consecutive_blocks < 1
            or (max_ads is not None and max_ads < 1)):
        raise ValueError("Проверьте диапазон страниц, лимиты и паузу")
    csv_path = os.fspath(csv_path or f"krisha_{slug}.csv")
    pending_path = csv_path + ".pending.json"
    seen = load_seen_urls(csv_path)
    pending = {url: reason for url, reason in load_pending(pending_path).items() if url not in seen}
    session = make_session(delay_range, delay_policy)
    buffer: List[FlatRow] = []
    attempted = set()
    total = skipped = errors = blocks = consecutive_blocks = empty_in_row = 0
    last_error = None
    status = "Диапазон страниц обработан"

    def flush():
        if buffer:
            # Удаляем URL из очереди только после успешной записи в CSV.
            append_rows_to_csv(csv_path, buffer)
            for row in buffer:
                pending.pop(row.url, None)
            buffer.clear()
        save_pending(pending_path, pending)

    def limit_reached():
        return max_ads is not None and total >= max_ads

    def collect(url):
        nonlocal total, skipped, errors, blocks, consecutive_blocks, last_error
        if url in seen or url in attempted:
            skipped += 1
            return
        attempted.add(url)
        try:
            html = fetch_html(session, url, block_pause=block_pause)
            consecutive_blocks = 0
            row = parse_advert_page(html, url)
        except AccessBlocked as e:
            errors += 1
            blocks += 1
            consecutive_blocks += 1
            last_error = str(e)
            pending[url] = last_error
            save_pending(pending_path, pending)
            if e.stop_run or consecutive_blocks >= max_consecutive_blocks:
                raise
            return
        except (requests.RequestException, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as e:
            errors += 1
            last_error = f"{url}: {e}"
            pending[url] = last_error
            save_pending(pending_path, pending)
            return
        buffer.append(row)
        seen.add(url)
        total += 1
        if len(buffer) >= save_every:
            flush()

    try:
        append_rows_to_csv(csv_path, [])
        save_pending(pending_path, pending)
        # Зафиксированные ранее ссылки не теряются при сдвиге выдачи.
        for url in list(pending):
            collect(url)
            if limit_reached():
                break

        if not limit_reached():
            for page in range(start_page, end_page + 1):
                list_url = f"{BASE}/prodazha/kvartiry/{slug}/?page={page}"
                try:
                    list_html = fetch_html(session, list_url, block_pause=block_pause)
                except AccessBlocked:
                    # Без выдачи нельзя надёжно выбрать следующие объявления.
                    raise
                except (requests.RequestException, RuntimeError) as e:
                    errors += 1
                    last_error = f"страница {page}: {e}"
                    continue
                urls = extract_listing_urls(list_html)
                if not urls:
                    empty_in_row += 1
                    if empty_in_row >= max_empty_pages:
                        status = f"Остановка после {max_empty_pages} пустых страниц"
                        break
                    continue
                empty_in_row = 0
                for url in urls:
                    if url not in seen:
                        pending.setdefault(url, "")
                save_pending(pending_path, pending)
                for url in urls:
                    collect(url)
                    if limit_reached():
                        break
                if verbose:
                    print(f"стр. {page}: собрано {total}, пропущено {skipped}, ошибок {errors}")
                if limit_reached():
                    break
        if limit_reached():
            status = f"Достигнут лимит {max_ads} новых объявлений"
    except AccessBlocked as e:
        status = f"Сбор остановлен: {e}"
    finally:
        try:
            flush()
        finally:
            session.close()
    details = f" Последняя ошибка: {last_error}." if last_error else ""
    queue_info = f" Осталось в очереди: {len(pending)} ({pending_path})." if pending else ""
    print(f"{status}. Новых: {total}, пропущено: {skipped}, ошибок: {errors} "
          f"(блокировок объявлений: {blocks}). CSV: {csv_path}.{queue_info}{details}")
    return csv_path

def parse_listing_page(page_html: str, city: str) -> List[ListingRow]:
    """Данные карточек выдачи. Никаких дополнительных HTTP-запросов."""
    soup = BeautifulSoup(page_html, "html.parser")
    rows = []
    seen = set()
    for card in soup.select(".a-card"):
        if card.find_parent("aside"):
            continue
        title = card.select_one("a.a-card__title[href]")
        if not title:
            continue
        url = normalize_ad_url(title["href"])
        if not re.fullmatch(r"https://krisha\.kz/a/show/\d+", url) or url in seen:
            continue
        seen.add(url)

        def text(selector):
            el = card.select_one(selector)
            return clean_text(el.get_text(" ", strip=True)) if el else None

        name = clean_text(title.get_text(" ", strip=True))
        preview = text(".a-card__text-preview")
        address = text(".a-card__subtitle")
        price_text = text(".a-card__price")
        price_match = re.fullmatch(
            r"(?:от\s+)?([\d\s]+(?:[.,]\d+)?)\s*(млн|тыс\.?)?\s*(?:₸|тг|тенге)",
            price_text or "", re.I,
        )
        price = None
        if price_match:
            amount = float(re.sub(r"\s", "", price_match[1]).replace(",", "."))
            unit = price_match[2] or ""
            price = round(amount * (1_000_000 if unit == "млн" else 1_000 if unit else 1))
        rooms = re.search(r"(\d+)\s*[-–]?\s*комнатн", name, re.I)
        area = re.search(r"(\d+(?:[.,]\d+)?)\s*м(?:²|2|\^2)", name)
        floor_match = re.search(r"(?<![\d.,])(\d+\s*(?:(?:/|из)\s*\d+)?\s*этаж)\b", name, re.I)
        floor, floor_count = parse_floor_text(floor_match[1]) if floor_match else (None, None)
        total_floors = re.search(r"(?:^|,\s*)(\d+)\s*этаж(?:ей|а)?\b", preview or "", re.I)
        if floor_count is None and total_floors:
            floor_count = int(total_floors[1])
        year = re.search(r"\b(\d{4})\s*г\.?\s*п\.?", preview or "", re.I)
        ceiling = re.search(r"потолки\s+(\d+(?:[.,]\d+)?)\s*м", preview or "", re.I)
        house = re.search(r"\b(кирпичный|панельный|монолитный|блочный|деревянный|каркасный)\s+дом\b", preview or "", re.I)
        bathroom = re.search(r"санузел\s+(совмещ[её]нный|раздельный|\d+\s*с/у(?:\s*и\s*более)?)", preview or "", re.I)
        district = re.match(r"([^,]+?\s+(?:р-н|район))(?=,|$)", address or "", re.I)
        complex_name = text('.a-card__owner a[href*="/complex/show/"]')
        if complex_name:
            complex_name = re.sub(r"^ЖК\s*", "", complex_name).strip('«»" ')
        else:
            complex_match = re.search(r"жил\.?\s*комплекс\s+([^,]+)", preview or "", re.I)
            complex_name = complex_match[1].strip() if complex_match else None
        for tooltip in card.select(".kr-tooltip"):
            tooltip.decompose()
        owner = text(".a-card__owner-label") or text(".a-card__owner")
        rows.append(ListingRow(
            city=city, name=name, district=district[1] if district else None,
            address=address, price=price, price_text=price_text,
            price_is_from=bool(re.match(r"от\b", price_text or "", re.I)),
            owner=owner, complex_name=complex_name, house_type=house[1].lower() if house else None,
            construction_year=int(year[1]) if year else None,
            ceiling_height=ceiling[1].replace(",", ".") + " м" if ceiling else None,
            bathroom_info=bathroom[1] if bathroom else None,
            area=float(area[1].replace(",", ".")) if area else None,
            room_count=int(rooms[1]) if rooms else None, floor=floor, floor_count=floor_count,
            url=url, information_preview=preview,
        ))
    if not rows and (soup.select_one(".a-card") or extract_listing_urls(page_html)):
        raise ValueError("Карточки найдены, но их разметка не распознана")
    return rows


def append_listing_rows(csv_path: str, rows: List[ListingRow]) -> None:
    fieldnames = [field.name for field in fields(ListingRow)]
    exists = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    if exists:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            if next(csv.reader(f), []) != fieldnames:
                raise ValueError("CSV имеет другой набор столбцов: выберите новый csv_path")
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def parse_krisha_listings(
    city: str,
    start_page: int = 1,
    end_page: int = 3000,
    csv_path: Optional[str] = None,
    max_ads: Optional[int] = None,
    max_empty_pages: int = 5,
    delay_range: Optional[Tuple[float, float]] = None,
    delay_policy: Optional[DelayPolicy] = None,
    verbose: bool = False,
) -> str:
    """Сбор только выдачи, без координат и переходов в объявления.

    max_ads ограничивает число НОВЫХ строк за запуск; None — весь диапазон.
    Краткое описание и доступные характеристики берутся из карточек выдачи.
    CSV сохраняется после каждой страницы. При блокировке сбор останавливается;
    повторный запуск пропускает URL, уже записанные в CSV.
    """
    slug = CITIES.get(city, city)
    if slug not in CITIES.values():
        raise ValueError(f"Неизвестный город {city!r}. Доступны: {', '.join(CITIES)}")
    if (start_page < 1 or end_page < start_page or max_empty_pages < 1
            or (max_ads is not None and max_ads < 1)):
        raise ValueError("Проверьте диапазон страниц и лимиты")
    city_name = next(name for name, value in CITIES.items() if value == slug)
    csv_path = os.fspath(csv_path or f"krisha_{slug}_listings.csv")
    append_listing_rows(csv_path, [])  # Проверка схемы до первого HTTP-запроса.
    seen = {normalize_ad_url(url) for url in load_seen_urls(csv_path)}
    session = make_session(delay_range, delay_policy)
    total = skipped = errors = empty = 0
    previous_urls = None
    buffer = []
    status = "Диапазон страниц обработан"
    last_error = None
    try:
        for page in range(start_page, end_page + 1):
            url = f"{BASE}/prodazha/kvartiry/{slug}/?page={page}"
            try:
                html = fetch_html(session, url)
                rows = parse_listing_page(html, city_name)
            except AccessBlocked as e:
                status = f"Сбор остановлен: {e}"
                errors += 1
                break
            except ValueError as e:
                status = f"Сбор остановлен: {e}"
                errors += 1
                break
            except (requests.RequestException, RuntimeError) as e:
                errors += 1
                last_error = f"страница {page}: {e}"
                continue
            if not rows:
                empty += 1
                if empty >= max_empty_pages:
                    status = f"Остановка после {max_empty_pages} пустых страниц"
                    break
                continue
            empty = 0
            page_urls = {row.url for row in rows}
            if page_urls == previous_urls:
                status = f"Сбор остановлен: страница {page} повторяет предыдущую"
                break
            previous_urls = page_urls
            for row in rows:
                if row.url in seen:
                    skipped += 1
                    continue
                buffer.append(row)
                seen.add(row.url)
                total += 1
                if max_ads is not None and total >= max_ads:
                    break
            append_listing_rows(csv_path, buffer)
            buffer.clear()
            if verbose:
                print(f"стр. {page}: новых {total}, пропущено {skipped}, ошибок {errors}")
            if max_ads is not None and total >= max_ads:
                status = f"Достигнут лимит {max_ads} новых объявлений"
                break
    finally:
        try:
            if buffer:
                append_listing_rows(csv_path, buffer)
        finally:
            session.close()
    detail = f" Последняя ошибка: {last_error}." if last_error else ""
    print(f"{status}. Новых: {total}, пропущено: {skipped}, ошибок: {errors}. CSV: {csv_path}.{detail}")
    return csv_path


if __name__ == "__main__":
    # Пример: python krisha_parser.py Шымкент
    import sys
    parse_krisha(sys.argv[1] if len(sys.argv) > 1 else "Шымкент")
