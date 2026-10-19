# Flöde – svenska, ett svep i taget

TikTok-tyylinen videofeed ruotsin opiskeluun. Videot haetaan mekaanisesti ruotsinkielisten
YouTube-kanavien RSS-syötteistä. Äppi lisää päälle napautettavan tekstityksen, käännökset,
sanapankin, visat ja päivän tavoitteen.

## Näin se toimii

- **Hakurobotti** (`scripts/build_feed.py`) pyörii GitHub Actionsissa 4 tunnin välein ja kerää
  videoita kolmesta lähteestä:
  1. **Kanavalista** (`channels.json › channels`): Shortsit ja pitkät videot kanavan omista
     soittolistoista.
  2. **Hakusanat** (`channels.json › searches`): YouTube-haku alle 4 minuutin videoille. Otsikon
     pitää näyttää ruotsilta, muuten video hylätään.
  3. **Automaattinen kanavalöytö:** kanava, joka osuu hakuihin toistuvasti ruotsinkielisillä
     videoilla, otetaan seurantaan. Lähteiden määrä kasvaa itsestään.
- Pitkistä videoista robotti leikkaa 40 sekunnin klipin **eniten uudelleen katsotusta kohdasta**
  (tai tekstityksen tauoista, jos tekstitys saatiin).
- **Kirjasto kertyy:** videot säilyvät ajosta toiseen (enintään 5000 korttia), joten feed kasvaa.
- **Äppi** (`site/index.html`) aukeaa suoraan videoon. Se ei näytä samaa videota uudelleen
  ennen kuin kaikki on katsottu, ja vaihtelee kanavia ja aiheita.

## Käyttöönotto (noin 15 minuuttia)

1. **Luo repo.** Mene osoitteeseen github.com/new, anna nimeksi esimerkiksi `flode`, valitse
   **Public** ja paina **Create repository**.
2. **Lataa tiedostot.** Paina tyhjässä repossa linkkiä **uploading an existing file** ja raahaa
   kaikki tämän kansion tiedostot ja kansiot sivulle. Paina **Commit changes**.
   - Tärkeää: kansio `.github` on piilotettu. Macissa saat piilotiedostot näkyviin Finderissa
     painamalla **Cmd + Shift + .** (piste). Windowsissa: Resurssienhallinta › Näytä ›
     Piilotetut kohteet.
   - Jos `.github` ei silti lähde mukaan: GitHubissa **Add file › Create new file**, kirjoita
     nimeksi `.github/workflows/feed.yml` ja liitä sisältö tämän paketin samannimisestä
     tiedostosta.
3. **Ota sivut käyttöön.** Repon **Settings › Pages › Build and deployment › Source:**
   valitse **GitHub Actions**.
4. **Käynnistä robotti.** Välilehti **Actions**. Jos GitHub kysyy, hyväksy työnkulut. Valitse
   vasemmalta **Päivitä feed ja julkaise** ja paina **Run workflow**. Ajo kestää 3–8 minuuttia.
5. **Avaa äppi.** Osoite on `https://KÄYTTÄJÄNIMESI.github.io/flode/`. Avaa se iPhonen
   Safarissa ja valitse **Jaa › Lisää Koti-valikkoon**. Äppi saa oman ikoninsa ja aukeaa
   koko näytölle ilman selaimen palkkeja.

## Kanavien ja hakusanojen muokkaus

Avaa GitHubissa `channels.json`, paina kynäkuvaketta ja muokkaa.
- **Kanava:** `handle` on YouTube-kanavan osoitteen @-nimi (esim. youtube.com/**@svtnyheter**).
- **Hakusana:** `q` on hakulause ruotsiksi (esim. `"enkel middag recept"`). Kirjoita hakuja
  ruotsiksi, niin tulokset ovat ruotsinkielisiä.
- `category` määrää välilehden. Uusi kategoria luo uuden välilehden automaattisesti.

Tallennus käynnistää robotin automaattisesti.

Robotin loki (Actions › viimeisin ajo › build › Rakenna feed.json) näyttää jokaisesta
kanavasta ✓ tai ✗. Jos kanava on ✗, sen @-nimi on väärin: tarkista se YouTubesta.

## Tekstitykset

Napautettava tekstitys vaatii, että robotti saa videon ruotsinkielisen tekstityksen.
YouTube estää välillä pilvipalvelimia, myös GitHubin. Silloin lokissa lukee
*”YouTube esti tekstitysten haun”*, ja videoissa näkyy YouTuben oma tekstitys, jota ei voi
napauttaa. Feed toimii silti normaalisti.

**Varasuunnitelma:** aja robotti kerran omalla koneellasi. Kotiliittymää YouTube ei yleensä
estä.

```
pip install youtube-transcript-api
python3 scripts/build_feed.py
```

Lataa syntynyt `site/feed.json` GitHubiin samaan kansioon. Robotti käyttää sen
tekstityksiä jatkossa pohjana eikä hae niitä uudelleen.

## Hyvä tietää

- GitHub keskeyttää ajastetut ajot, jos repoon ei ole tehty muutoksia 60 päivään. Ne saa
  takaisin päälle Actions-välilehdeltä (**Enable workflow**).
- Kaikki oma data (tykkäykset, sanat, pisteet) on vain puhelimessasi.
- Käännökset haetaan Google Kääntäjästä vain silloin, kun napautat sanaa tai otat
  Sv+Fi-tilan käyttöön.
- Videot toistetaan YouTuben omalla soittimella. Tarkoitettu henkilökohtaiseen käyttöön.

## Pikaohje äpin käyttöön

- **Pyyhkäise ylös:** seuraava video. **Napauta:** tauko/toisto. **Tuplanapauta:** tykkää.
- **Napauta tekstityksen sanaa:** käännös, koko repliikin käännös, ääntämys ja tallennus.
- **Text:** Sv → Sv+Fi → Av (korvatreeni). **Tempo:** 1× → 0,75× → 0,5×. **Igen:** toista repliikki.
- **Rengas oikealla ylhäällä:** päivän tavoite, sanapankki ja asetukset.
- Tykkäykset, katseluaika ja ohitukset opettavat feedille, mitä haluat nähdä. Taso mukautuu
  sen mukaan, kuinka paljon napautat sanoja.
