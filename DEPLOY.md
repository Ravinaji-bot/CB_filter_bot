# Auto Filter Bot — Koyeb Deployment Guide

## 1. Code GitHub par push karo
Ye poora folder (bot.py, requirements.txt, Dockerfile) ek GitHub repo me daalo.

## 2. Koyeb par naya app banao
1. https://app.koyeb.com par login karo
2. Create App → GitHub select karo → apni repo choose karo
3. Build method: Dockerfile (auto-detect ho jayega)
4. Service type: Web Service
5. Port: 8000

## 3. Environment Variables (Koyeb dashboard → Environment Variables)

API_ID            - apna Telegram API ID (my.telegram.org se)
API_HASH          - apna API Hash
BOT_TOKEN         - BotFather se mila token
BOT_USERNAME      - bot ka username, bina @ ke
DB_CHANNEL        - apne database channel ki ID (-100...)
POST_CHANNEL      - jis channel me auto-post jayega uski ID
MONGO_URI         - MongoDB Atlas connection string
TMDB_API_KEY      - themoviedb.org se free API key
WATCH_REGION      - OTT provider ke liye country code (default IN)
POWERED_BY_TEXT   - post ke neeche dikhne wala text
POWERED_BY_LINK   - us text ka link
REQUEST_CHANNEL   - private channel jahan movie requests aayengi
OWNER_ID          - aapka apna Telegram user ID (zaroori hai)
SPAM_LOG_CHANNEL  - private channel jahan spam alerts aayenge
MUTE_MINUTES      - kitni der mute karna hai (default 30)
LOG_CHANNEL       - private channel jahan naye users/groups ka log aayega
TOTAL_STORAGE_MB  - MongoDB cluster ka total storage quota (default 512)
FORCE_SUB_CHANNEL - private channel jise join kiye bina bot use nahi hoga

Ye values kabhi bhi seedhe code me mat likho — hamesha environment variables se hi daalo.

## 4. Deploy
Deploy dabao. Logs me "Bot starting..." dikhega matlab bot chal raha hai.
Apne Koyeb app ka URL khol ke check karo — "Auto Filter Bot is running!" dikhna chahiye.

## 5. Zaroori setup steps
- Bot ko DB_CHANNEL me admin banao (files padhne ke liye)
- Bot ko POST_CHANNEL me bhi admin banao (post karne + edit karne ke liye)
- Bot ko jis group me use karna hai wahan add karo
- BOT_USERNAME sahi hona chahiye warna deep-links kaam nahi karenge

## Auto Poster kaise kaam karta hai
Jab bhi DB_CHANNEL me naya video/file aata hai, bot uske caption/filename se quality,
source, audio languages detect karta hai, TMDB se genre/OTT/poster fetch karta hai, aur
POST_CHANNEL me ek structured post bana deta hai. Har quality ke saamne ek "Click Here"
link hota hai jo us exact file ka deep-link hai. Same movie ki nayi quality baad me aaye
to purana post edit ho jaata hai, naya post nahi banta.

## Poster
Poster hamesha landscape (backdrop) hota hai, HD quality me. Bot poster ko khud download
karke upload karta hai taaki original TMDB link kahin expose na ho.

## Force Subscribe
FORCE_SUB_CHANNEL set karne par, jab tak user us private channel ko join nahi karta, bot
use nahi kar payega. Channel me "Approve New Members" setting ON honi chahiye taaki
join-request wala flow chale.

## Live /stats Command (owner-only)
OWNER_ID wala user /stats bhejega to users, groups, files, storage, uptime, RAM, CPU ka
live data dikhega.

## User & Group Logging
Naya user /start kare ya bot kisi naye group me add ho, to LOG_CHANNEL me turant details
chali jaati hain.

## Anti-Spam System
Group me link/@mention/"bot" keyword/lamba spam word bhejne par message auto-delete hota
hai, warning milti hai (1/3, 2/3, 3/3), aur 4th baar par MUTE_MINUTES ke liye mute kar
diya jaata hai. Har event SPAM_LOG_CHANNEL me log hota hai. Group admins aur owner is
check se exempt hote hain.

## Request Your Movie Button
Har post ke niche button hota hai jispar click karke member movie/series request kar
sakta hai, jo seedhe REQUEST_CHANNEL (private, sirf admin ke liye) me chali jaati hai.

## Security — Owner-authorized Groups
Bot sirf un groups me kaam karta hai jinhe owner ne /addgroup se authorize kiya ho.
Random group me add hote hi, agar authorize nahi hai to bot warning dekar khud leave kar
leta hai. Commands: /addgroup, /removegroup, /listgroups (sabhi owner-only).

## Broadcast
Owner kisi message par reply karke /broadcast bhejega to wo sabhi users aur groups me
chala jaata hai. /setdeletetime <minutes> se auto-delete time set hota hai (0 = off).

## Web Series Support
Caption me S01 se S20 tak season number mile to bot use series samajhta hai. Episode
range (E01-E08 wagera) auto-detect hoti hai, har season ka alag post banta hai, quality
wahi dikhti hai jo caption me likhi hai.

## Naye tags add karne ho to
QUALITY_PATTERNS, SOURCE_PATTERNS, LANGUAGES, GENRE_MAP — inme bot.py ke andar easily
naye patterns/values add kar sakte ho.
