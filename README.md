# AZ News Monitor — v1

Azərbaycan xəbər saytlarının RSS lentlərini 5 dəqiqədən bir yoxlayan, açar sözlərə görə filtrləyən, uyğun yeni xəbərləri veb paneldə göstərən və Telegram-a göndərən öz serverinizdə işləyən layihə.

## 1. Lokal quraşdırma (Windows)

1. Python 3.12 quraşdırın.
2. Qovluqda terminal açın və `python -m venv .venv` yazın.
3. `.venv\\Scripts\\activate` və `pip install -r requirements.txt` icra edin.
4. `.env.example` faylını `.env` adı ilə kopyalayın. `ADMIN_TOKEN` üçün uzun təsadüfi parol təyin edin. `DATABASE_PATH=news.db` yazın.
5. `uvicorn app:app --host 127.0.0.1 --port 8000` icra edin.
6. `http://127.0.0.1:8000` ünvanını açın, ADMIN_TOKEN daxil edin və əvvəlcədən əlavə olunmuş açar sözlərə baxın.

## 2. Telegram bildirişləri

1. Telegram-da @BotFather ilə bot yaradın və tokeni `.env` faylındakı `TELEGRAM_BOT_TOKEN` dəyərinə yazın.
2. Botunuza `/start` göndərin.
3. `https://api.telegram.org/bot<TOKEN>/getUpdates` cavabından şəxsi və ya qrup `chat.id` dəyərini götürüb `TELEGRAM_CHAT_ID` sahəsinə yazın. Tokeni başqaları ilə paylaşmayın.
4. Proqramı yenidən başladın.

## 3. VPS-də 24/7 Docker quraşdırması

Ubuntu VPS-də Docker Engine və Docker Compose plugin quraşdırın. Layihəni serverə köçürün. `.env.example` faylını `.env` olaraq kopyalayın, `ADMIN_TOKEN`, Telegram parametrlərini doldurun. `DATABASE_PATH=/data/news.db` saxlayın.

```
docker compose up -d --build
docker compose logs -f monitor
```

Panel təhlükəsizlik üçün yalnız serverin `127.0.0.1:8000` ünvanında dinləyir. Öz kompüterinizdən SSH tuneli açın:

```
ssh -L 8000:127.0.0.1:8000 USER@SERVER_IP
```

Sonra `http://127.0.0.1:8000` açın. İctimai domen üçün HTTPS reverse proxy və əlavə giriş mühafizəsi qurun; 8000 portunu birbaşa internetə açmayın.

## 4. Mənbələr və əhatə

`sources.json` faylında RSS URL-ləri və adlar var. **Nümunə RSS ünvanları canlı yoxlanılmayıb**: istifadədən əvvəl brauzerdə yoxlayın və işləməyənləri aktual RSS ünvanları ilə dəyişin. Saytın RSS-i yoxdursa, həmin sayt üçün ayrıca icazəli adapter hazırlanmalıdır. Bütün Azərbaycan saytlarını avtomatik əhatə etmir.

## 5. Vacib davranışlar

- Açar sözlər DSMF, pensiya, müavinət, sosial yardım və əlaqəli ifadələrlə əvvəlcədən doldurulub. İlk işə düşmədə RSS-dəki mövcud xəbərlər də yeni sayılaraq bildiriş yarada bilər.
- Yeni açar sözlər əlavə ediləndən sonra yalnız həmin andan etibarən ilk dəfə verilənlər bazasına düşən xəbərlər bildiriş yaradır. Əvvəl saxlanmış xəbərlər geriyə dönük analiz olunmur.
- Təkrar bildirişlərin qarşısı eyni URL əsasında alınır; müxtəlif URL-lərdə eyni məqalə üçün semantik deduplikasiya yoxdur.
- Axtarış başlıq və RSS xülasəsində aparılır, məqalənin tam mətnində deyil.
- Bot bildiriş göndərir, lakin Telegram komandaları ilə idarəetmə hələ yoxdur; açar sözlər veb paneldən idarə edilir.
- 5 dəqiqə yoxlama dövrüdür, xəbərin 5 dəqiqəyə mütləq tapılacağına zəmanət deyil.
- RSS saytlarının istifadə şərtlərinə, robot qaydalarına və sorğu limitlərinə əməl edin.
- Bu ilkin versiya tək prosesli işləmə üçün nəzərdə tutulub; birdən çox worker işə salmayın.
- İstehsal mühitində daha güclü autentifikasiya, audit, ehtiyat nüsxələr, mənbə sağlamlığı və xəbər saxlama siyasəti əlavə edilməlidir.

## Bu buraxılış
DSMF üçün ilkin açar sözlər avtomatik əlavə edilir. Server və Telegram hesabı qoşulmadan 24/7 onlayn xidmət aktiv deyil.
