<!--
LAENULEPINGU MALL — Väline erainvestor (laenuandja) — v2-et (MUSTAND)

Staatus: MUSTAND. See on ingliskeelse, õigusnõustaja poolt üle vaadatud malli
(loan_agreement_external_v2.md, v2) eestikeelne tõlge. Tõlge ise EI OLE veel
Eesti õigusnõustaja poolt üle vaadatud, mistõttu sellel on versioonitähis, mis
lõpeb "-draft", ja Bruno renderdab selle MUSTANDI bänneri/vesimärgiga
(agreements.is_template_reviewed). Kui Eesti õigusnõustaja on tõlke üle
vaadanud, eemaldage TEMPLATE_VERSIONS["et"] väärtusest "-draft" järelliide,
täpselt nagu tehti ingliskeelse versiooniga 2026-06-06.

Malli keel: eesti keel (laenuandjale esitatav). Toodanguversioon on kakskeelne
eesti + inglise; see on eestikeelne pool. Ingliskeelne tõlge on paralleelselt
hooldatav (loan_agreement_external_v2.md). Lahknevuse korral kehtib eestikeelne
versioon (§15.6).

Muutujad {{ kahekordsetes sulgudes }} täidab Bruno uue laenu vormilt. Muutujad
{{ counterparty.* }} täidetakse laenuandja vastaspoole kirjest. Muutujate nimed
on ingliskeelse malliga identsed, et üks lahendatud kontekst renderdaks mõlemat.

Reateksti "LAWYER REVIEW:" märkmed all on säilitatud kui ülevaate jälg; need
eemaldatakse renderdatud väljundist ega jõua laenuandjale esitatavasse
dokumenti. (See lause ei tohi sisaldada kommentaari sulgemismärki, vastasel
juhul sulguks see plokk enneaegselt.)
-->

# LAENULEPING

**Sõlmitud**

**MesiCap Technologies OÜ** ("Laenusaaja")
Eesti äriregistri kood 17323813
Registreeritud asukoht: Suur-Liiva tn 15-13, Haapsalu linn, 90503, Eesti
Esindaja: {{ borrower.represented_by }}

**ja**

**{{ counterparty.name }}** ("Laenuandja")
{% if counterparty.type == 'company' %}
Registrikood: {{ counterparty.registration_number }}
Õiguslik vorm: {{ counterparty.legal_form }}
{% endif %}
Aadress: {{ counterparty.address }}
{% if counterparty.contact_email %}E-post: {{ counterparty.contact_email }}{% endif %}
{% if counterparty.represented_by %}Esindaja: {{ counterparty.represented_by }}{% endif %}

(kumbki "Pool", koos "Pooled") vahel

Lepingu kuupäev: {{ contract_date }}
Allakirjutamise koht: {{ place_of_signing }}

---

## 1. Ese ja põhisumma

1.1. Laenuandja kohustub andma Laenusaajale laenu põhisummas **{{ principal_max | currency_words }} ({{ principal_formatted }} {{ currency }})** ("Laen").

1.2. Laen antakse järgmisel eesmärgil: {{ purpose_description }}. Laen on struktureeritud kui {{ terms_summary }}. Laenusaaja võib kasutada laenusummat üldisteks majandustegevuse eesmärkideks kooskõlas selle eesmärgiga, sealhulgas, kuid mitte ainult käibekapitaliks, investeerimistegevuseks ja tegevuskuludeks.

1.3. Laen on **mitteülekantav**. Laenuandja ei tohi loovutada, müüa, pantida ega muul viisil üle anda käesolevat Laenu ega ühtegi sellest tulenevat õigust kolmandale isikule ilma Laenusaaja eelneva kirjaliku nõusolekuta. Iga selline väidetav üleandmine ilma nimetatud nõusolekuta on tühine.

---

## 2. Väljamakse

2.1. Laenuandja kannab põhisumma Laenusaaja kontole AS-is LHV Pank (IBAN: EE187700771012126780) hiljemalt {{ origination_date }} ("Väljamakse kuupäev").

2.2. Väljamakse tingimuseks on:
 (a) käesoleva Lepingu allkirjastamine mõlema Poole poolt;
 (b) Laenuandja on esitanud Jaotise 12 kohaselt nõutava vahendite päritolu ja tegelike kasusaajate dokumentatsiooni;
 (c) Laenusaaja olukorras ei ole allakirjutamise ja väljamakse vahel toimunud olulist negatiivset muutust. Oluline negatiivne muutus tähendab muutust, mis oluliselt kahjustab Laenusaaja võimet täita oma maksekohustusi käesoleva Lepingu alusel.

2.3. Väljamakse tegemata jätmine kuupäevaks {{ origination_date }} annab Laenusaajale õiguse käesolev Leping ilma leppetrahvita lõpetada.

---

## 3. Intress

3.1. Laenult arvestatakse intressi määraga **{{ interest_rate_pct_formatted }}% aastas** ({{ interest_rate_type }}), arvutatuna {{ day_count_convention }} päevade arvestuse konventsiooni alusel.

3.2. Intress hakkab kogunema Väljamakse kuupäevast kuni Laenu täieliku tagasimaksmiseni.

{% if interest_treatment == 'capitalizing' %}
3.3. Intress **kapitaliseerub**: kogunenud intress liidetakse põhisummale igapäevaselt ning muutub osaks võlgnetavast põhisummast. Kogu summa (põhisumma koos kapitaliseeritud intressiga) makstakse tagasi Lõpptähtajal vastavalt Jaotisele 4.
{% elif interest_treatment == 'paid_periodically' %}
3.3. Intressi makstakse **perioodiliselt** rahas. Intressimaksed kuuluvad tasumisele {{ payment_frequency }}, hiljemalt iga {{ payment_frequency_unit }} {{ payment_day_of_month }}. kuupäeval. Esimene intressimakse kuulub tasumisele {{ first_interest_payment_date }}.

3.4. Hilinenud intressimaksetelt arvestatakse viivisintressi määraga {{ default_rate_pct_formatted }}% aastas (lepinguline määr pluss 2 protsendipunkti) kuni tasumiseni.
{% elif interest_treatment == 'amortizing' %}
3.3. Laen on **amortiseeruv**: põhisumma ja intress makstakse tagasi võrdsetes osamaksetes vastavalt Jaotise 4 graafikule.
{% endif %}

3.5. Intressi arvutatakse tegelikult tasumata põhisumma jäägilt.

---

## 4. Tagasimaksmine

{% if repayment_structure == 'bullet' %}
4.1. Laen makstakse tagasi ühe maksega {{ maturity_date }} ("Lõpptähtaeg"). Tagasimakstav summa võrdub tasumata põhisummaga, millele lisandub selleks kuupäevaks kogunenud ja tasumata intress.

4.2. Laenusaaja kannab tagasimakstava summa Laenuandja määratud kontole (IBAN: {{ counterparty.iban }}) hiljemalt Lõpptähtajaks.

{% elif repayment_structure == 'amortizing' %}
4.1. Laen makstakse tagasi {{ installment_count }} võrdses igakuises osamakses suurusega **{{ installment_formatted }} {{ currency }}** igaüks, mis kuuluvad tasumisele iga kuu {{ payment_day_of_month }}. kuupäeval alates {{ first_payment_date }}.

4.2. Viimane osamakse kuulub tasumisele {{ maturity_date }}.

4.3. Iga osamakse katab põhisumma ja intressi vastavalt standardsele amortisatsioonigraafikule, mis on lisatud Lisana A.
{% endif %}

4.4. Kõik maksed tehakse {{ currency }} pangaülekandega Laenuandja määratud kontole. Pangaülekande kulud kannab Laenusaaja.

---

## 5. Ennetähtaegne tagasimaksmine

{% if early_repayment_allowed %}
5.1. Laenusaaja võib Laenu igal ajal täielikult või osaliselt tagasi maksta, järgides {{ early_repayment_notice_days }}-päevast eelnevat kirjalikku etteteatamist Laenuandjale.

5.2. Ennetähtaegne tagasimaksmine hõlmab:
 (a) tagasimakstavat põhisummat;
 (b) ennetähtaegse tagasimaksmise kuupäevani kogunenud ja tasumata intressi;
 (c) ilma ennetähtaegse tagasimaksmise leppetrahvi või preemiata.
{% else %}
5.1. Ennetähtaegne tagasimaksmine ei ole käesoleva Lepingu alusel lubatud, välja arvatud Laenuandja eelneval kirjalikul nõusolekul. Laenuandjal puudub kohustus sellist nõusolekut anda.
{% endif %}

---

## 6. Kinnitused ja tagatised

6.1. Laenusaaja kinnitab ja tagab, et:
 (a) ta on nõuetekohaselt asutatud ja kehtivalt tegutsev Eesti õiguse alusel;
 (b) tal on täielik äriühinguõiguslik pädevus käesoleva Lepingu sõlmimiseks;
 (c) käesolev Leping moodustab Laenusaaja jaoks seadusliku, kehtiva ja siduva kohustuse;
 (d) käesoleva Lepingu kuupäeva seisuga ei ole toimunud ega kesta ükski rikkumise juhtum (nagu määratletud Jaotises 7);
 (e) Laenusaaja viimane Laenuandjale avaldatud finantsseisund on kõigis olulistes aspektides täpne.

6.2. Laenuandja kinnitab ja tagab, et:
 (a) tal on seaduslik õigus käesolev Leping sõlmida;
 (b) laenatavad vahendid pärinevad seaduslikest allikatest ja kuuluvad tegelikult Laenuandjale (või Jaotise 12 kohases tegelike kasusaajate dokumentatsioonis avaldatud isikutele);
 (c) Laen ei kujuta endast ühegi kuritegeliku tegevuse tulu;
 (d) Laenuandja on saanud kõik oma jurisdiktsiooni õiguse kohaselt käesoleva Laenu andmiseks nõutavad heakskiidud või nõusolekud.

---

## 7. Rikkumise juhtumid

7.1. Iga järgnev moodustab "Rikkumise juhtumi":
 (a) Laenusaaja jätab tasumata mis tahes käesoleva Lepingu alusel tasumisele kuuluva summa selle tähtpäeval ning selline tasumata jätmine kestab {{ default_cure_days | default(15) }} tööpäeva pärast Laenuandja kirjalikku teadet;
 (b) Laenusaaja rikub mis tahes olulist käesoleva Lepingu kinnitust, tagatist või kohustust ning selline rikkumine (kui see on kõrvaldatav) jääb kõrvaldamata {{ default_cure_days | default(15) }} tööpäeva jooksul pärast kirjalikku teadet;
 (c) Laenusaaja algatab vabatahtliku maksejõuetusmenetluse või tema suhtes algatatakse sundmaksejõuetusmenetlus, mida ei lõpetata 60 päeva jooksul;
 (d) Laenusaaja lõpetab sisuliselt kogu oma äritegevuse;
 (e) Laenusaaja vastu tehakse kohtuotsus summas üle 50 000 euro ning seda ei rahuldata ega peatata 60 päeva jooksul.

7.2. Laenuandja poolt Rikkumise juhtumi väljakuulutamata jätmine ei kujuta endast Laenuandja õigustest loobumist.

<!-- LAWYER REVIEW: Eesti pankrotiseadusel ja saneerimisseadusel on konkreetsed
tähtaja- ja künnisesätted. Kõrvaldamistähtajad, kohtuotsuste künnised ja
"olulisuse" määratlused tuleks viia kooskõlla seadusjärgsete vaikeväärtustega. -->

---

## 8. Ennetähtaegseks muutmine ja õiguskaitsevahendid

8.1. Kestva Rikkumise juhtumi korral:
 (a) võib Laenuandja Laenusaajale esitatud kirjaliku teatega kuulutada kogu tasumata põhisumma ja kogunenud intressi viivitamatult sissenõutavaks ja tasumisele kuuluvaks;
 (b) võib Laenuandja kasutada mis tahes muid käesoleva Lepingu, Eesti õiguse või õigluse alusel saadaolevaid õigusi ja õiguskaitsevahendeid.

8.2. Laenuandja õiguskaitsevahendid on kumuleeruvad ja mitteainuõiguslikud. Ühe õiguskaitsevahendi kasutamine ei välista ühtegi teist.

<!-- LAWYER REVIEW: ennetähtaegseks muutmise sätted peavad olema kooskõlas Eesti
võlaõigusseaduse lepingu lõpetamise ja kahju hüvitamise sätetega. -->

---

## 9. Kohustused

9.1. Senikaua kui käesoleva Lepingu alusel on mis tahes summa tasumata, on Laenusaaja kohustatud:
 (a) säilitama oma äriühingu olemasolu heas seisundis;
 (b) järgima kõigis olulistes aspektides kõiki kohaldatavaid õigusakte, sealhulgas Eesti äri-, maksu- ja rahapesuvastaseid õigusakte;
 (c) pidama täpset raamatupidamist ja dokumentatsiooni oma finantsseisundi kohta;
 (d) esitama Laenuandjale kvartaalsed finantskokkuvõtted kuuekümne (60) päeva jooksul pärast iga kalendrikvartali lõppu, mis hõlmavad: (i) tasumata laenukohustusi kõigi laenuandjate ees; (ii) jooksvat netovara väärtust; ja (iii) mis tahes olulisi muutusi Laenusaaja olukorras. Laenusaaja ei ole käesoleva Jaotise tähenduses kohustatud koostama auditeeritud finantsaruandeid ega avaldama konfidentsiaalset äriteavet, varalisi kauplemisstrateegiaid, kliendipõhist teavet, töötajate tasustamise teavet ega muud äriliselt tundlikku teavet;
 (e) teavitama Laenuandjat viivitamatult igast Rikkumise juhtumist või igast asjaolust, mis võib mõistlikult eeldatavalt põhjustada Rikkumise juhtumi.

9.2. Senikaua kui mis tahes summa on tasumata, ei tohi Laenusaaja ilma Laenuandja eelneva kirjaliku nõusolekuta:
 (a) võtta uut tagatud kõrgema nõudeõiguse järguga võlga, mis seataks käesolevast Laenust ettepoole;
 (b) teha väljamakseid, dividende või osade tagasiostmisi, mis põhjustaksid Laenusaaja netovara languse alla {{ minimum_net_worth | default("pooleteistkordse (1,5x) tasumata Laenu põhisumma") }};
 (c) ühineda teise üksusega või lasta end selle poolt omandada (välja arvatud täielikult omatava tütarettevõtte struktuur).

<!-- LAWYER REVIEW: kohustus (a) "tagatud kõrgema nõudeõiguse järguga võlg" —
täpsustada. Kohustus (b) — netovara määratlus peab vastama Bruno
omakapitalivaru arvutusele (koormamata vara brutoväärtus miinus võlakohustused
jne). -->

---

## 10. Aruandlus ja teabeõigused

10.1. Laenusaaja annab Laenuandjale juurdepääsu spetsiaalsele laenuandja portaalile (praegu kavandatud asukohaga lender.mesicap.com), kus Laenuandja saab seoses käesoleva Laenu ja Laenusaaja üldise finantsseisundiga vaadata:
 (a) jooksvat tasumata põhisummat ja kogunenud intressi;
 (b) maksete ajalugu;
 (c) Laenusaaja koondvõla ja vara suhtarvu;
 (d) Jaotise 9 kohaste kohustuste täitmise testide staatust.

10.2. Laenusaaja teeb mõistlikke jõupingutusi portaali ajakohasena hoidmiseks. Portaali kaudu esitatud teave on üksnes Laenuandja teavitamiseks ega muuda ega asenda käesolevat Lepingut.

10.3. Laenuandja teabeõigused ei laiene Laenusaaja konfidentsiaalsele äriteabele (nt konkreetsed kauplemispositsioonid, kliendinimekirjad, töötajate tasustamine).

---

## 11. Allutatuse kinnitus

11.1. Laenuandja tunnistab ja nõustub, et käesolev Laen on maksejärjekorras **kõrgema nõudeõiguse järguga** võrreldes mis tahes laenudega, mille Laenusaajale on andnud Laenusaaja osanikud või osanikega seotud üksused (koos "Osanikulaenud"). Osanikulaenud on käesolevale Laenule allutatud.

11.2. Laenusaaja mis tahes likvideerimisel, maksejõuetuse korral või lõpetamisel:
 (a) makstakse Laenuandjale (koos mis tahes teiste kõrgema nõudeõiguse järguga väliste laenuandjatega) täielikult enne mis tahes makse tegemist Osanikulaenudelt;
 (b) Osanikulaenud saavad makse üksnes varast, mis jääb järele pärast kõigi kõrgema nõudeõiguse järguga väliste laenuandjate rahuldamist.

11.3. Laenusaaja kinnitab, et käesoleva Lepingu kuupäeva seisuga moodustavad Osanikulaenud põhisummas ligikaudu {{ shareholder_loan_aggregate | default("[täidetakse koostamise ajal]") }} {{ shareholder_loan_currency | default(currency) }} kokku {{ shareholder_loan_count | default("[täidetakse koostamise ajal]") }} laenulepingu ulatuses, mis kõik on või saavad olema vastavate osanikest laenuandjatega sõlmitud allutamislepingute kaudu käesolevale Laenule formaalselt allutatud.

<!-- LAWYER REVIEW: Jaotis 11 on laenuandja kaitse ja Bruno LTV-arvutuse jaoks
kriitiline. Allutatus peab olema Eesti maksejõuetusõiguses jõustatav.
Kontrollida, et Bruno andmed osanikulaenude kohta kajastavad koostamise ajal
allutatuse staatust täpselt. -->

---

## 12. Rahapesu tõkestamine, vahendite päritolu ja tegelikud kasusaajad

12.1. Laenuandja kinnitab, et käesoleva Lepingu alusel laenatavad vahendid:
 (a) pärinevad seaduslikest allikatest;
 (b) ei kujuta endast kuritegelikku tulu;
 (c) ei ole mis tahes pooleliolevate kohtumenetluste, nõuete ega koormatiste objektiks, mis takistaksid Laenuandjal nende laenamist.

12.2. Laenuandja esitab Laenusaajale enne väljamakset:
 (a) füüsiliste isikute puhul: passi või ID-kaardi koopia, elukohatõendi, kirjaliku deklaratsiooni vahendite päritolu kohta koos tõendava dokumentatsiooniga;
 (b) juriidiliste isikute puhul: asutamistunnistuse, tegelike kasusaajate nimekirja (iga füüsiline isik, kes omab ≥ 25%), volitatud esindaja ID koopia, kirjaliku deklaratsiooni vahendite päritolu kohta;
 (c) mis tahes täiendava dokumentatsiooni, mida Laenusaaja mõistlikult nõuab Eesti rahapesuvastaste kohustuste täitmiseks.

12.3. Laenusaaja säilitab seda dokumentatsiooni Laenu kestuse jooksul ning viis aastat pärast seda, kooskõlas Eesti rahapesu ja terrorismi rahastamise tõkestamise seadusega.

12.4. Laenuandja kohustub esitama ajakohastatud dokumentatsiooni mõistliku nõudmise korral, kui asjaolud muutuvad (nt Laenuandja üksuse tegelike kasusaajate muutumine).

<!-- LAWYER REVIEW: Jaotis 12 peab olema kooskõlas Eesti rahapesu tõkestamise
seaduse tekstiga. Eelkõige tegelike kasusaajate avaldamise künnised ja
dokumentide säilitamise tähtaeg. -->

---

## 13. Kohaldatav õigus ja vaidluste lahendamine

13.1. Käesolevat Lepingut reguleerib Eesti Vabariigi õigus, välja arvatud selle rahvusvahelise eraõiguse normid.

13.2. Iga käesolevast Lepingust tulenev või sellega seotud vaidlus lahendatakse Eesti kohtutes, kusjuures **Harju Maakohtul** on ainupädevus esimese astme kohtuna.

13.3. Olenemata Jaotisest 13.2 võivad Pooled vastastikusel kirjalikul kokkuleppel suunata mis tahes konkreetse vaidluse vahekohtumenetlusse Eesti Kaubandus-Tööstuskoja arbitraažikohtu reeglite alusel, kusjuures sellisel juhul on vahekohtumenetluse asukoht Tallinn, keel eesti keel (või kokkuleppel inglise keel) ning vahekohus koosneb ühest vahekohtunikust, kui Pooled ei lepi kokku teisiti.

<!-- LAWYER REVIEW: Kontrollida kohtualluvust Eesti mitteresidendist
laenuandjate puhul. Mõni laenuandja võib soovida teistsugust kohtualluvust; see
mustand eeldab Eesti eelistust. -->

---

## 14. Teated

14.1. Kõik käesoleva Lepingu alusel esitatavad teated, taotlused ja muud teadaanded peavad olema kirjalikus vormis ja edastatud:
 (a) käsipostiga, kättesaamise kinnitusega;
 (b) tähitud kirjaga vastuvõtuteatisega;
 (c) e-postiga allpool nimetatud aadressile, lugemiskinnituse või vastuse kaudu kinnitusega.

14.2. Teadete aadressid:

**Laenusaajale:**
MesiCap Technologies OÜ
Suur-Liiva tn 15-13, Haapsalu linn, 90503, Eesti
E-post: {{ borrower.notice_email }}

**Laenuandjale:**
{{ counterparty.name }}
{{ counterparty.address }}
{% if counterparty.contact_email %}E-post: {{ counterparty.contact_email }}{% endif %}

14.3. Kumbki Pool võib oma teadete aadressi muuta, teatades sellest teisele Poolele kirjalikult.

---

## 15. Muud sätted

15.1. **Terviklik kokkulepe.** Käesolev Leping koos kõigi lisade ja kõigi hilisemate mõlema Poole allkirjastatud kirjalike muudatustega moodustab Poolte vahelise tervikliku kokkuleppe Laenu osas ning asendab kõik varasemad läbirääkimised, kinnitused ja kokkulepped.

15.2. **Muutmine.** Käesolevat Lepingut võib muuta üksnes mõlema Poole allkirjastatud kirjaliku dokumendiga. Muudatused salvestatakse Laenusaaja laenuhaldussüsteemis osana Laenu auditijäljest.

15.3. **Sätete eraldatavus.** Kui mõni käesoleva Lepingu säte tunnistatakse kehtetuks või jõustamatuks, jäävad ülejäänud sätted täielikult kehtima.

15.4. **Loobumine.** Kummagi Poole suutmatus või viivitus mis tahes õiguse kasutamisel ei kujuta endast sellest õigusest loobumist. Iga loobumine peab olema jõustamiseks kirjalik.

15.5. **Eksemplarid.** Käesoleva Lepingu võib allkirjastada eksemplarides (sealhulgas elektroonilistes eksemplarides), millest igaüks on originaal.

15.6. **Keel.** Käesolev Leping on koostatud nii eesti kui ka inglise keeles. **Lahknevuse korral kehtib eestikeelne versioon.**

15.7. **Konfidentsiaalsus.** Kumbki Pool hoiab käesoleva Lepingu tingimusi konfidentsiaalsena, välja arvatud juhul, kui seadus, määrus või kohtumäärus seda nõuab, või teise Poole kirjalikul nõusolekul. Laenuandja võib avaldada Laenu olemasolu ja põhitingimusi oma maksunõustajatele, audiitoritele ja tegelikele kasusaajatele teadmisvajaduse alusel.

15.8. **Vääramatu jõud.** Kumbki Pool ei loeta rikkuvat käesolevat Lepingut ega olevat mis tahes kohustuse täitmisega viivituses (välja arvatud juba sissenõutavaks muutunud maksekohustused) ulatuses, milles täitmist takistavad, viivitavad või muudavad ebamõistlikuks tema mõistliku kontrolli alt väljas olevad asjaolud, sealhulgas loodusõnnetused, sõda, rahutused, valitsuse tegevus, sanktsioonid, panga- või maksesüsteemide tõrked, sidekatkestused, küberintsidendid või sarnased sündmused. Iga mõjutatud tähtaeg pikeneb automaatselt sellise sündmuse kestuse võrra ning mõjutatud Pool teavitab teist Poolt nii ruttu kui mõistlikult võimalik.

15.9. Kumbki Pool ei vastuta mingil juhul käesolevast Lepingust tulenevate kaudsete, järelduslike, eriliste ega karistuslike kahjude eest.

15.10. **"Netovara"** tähendab Laenusaaja koguvara, millest on maha arvatud kogukohustused, arvutatuna konsolideerimata alusel kooskõlas Laenusaaja tavapärase ja järjepidevalt rakendatava raamatupidamistavaga, nagu kajastatud viimases juhtkonna aruandes.

---

## Allkirjad

**Laenusaaja nimel:**

MesiCap Technologies OÜ

_____________________________
{{ borrower.represented_by }}
Ametinimetus: {{ borrower.title }}
Kuupäev: ___________
Koht: ___________

**Laenuandja nimel:**

{{ counterparty.name }}

_____________________________
{{ counterparty.represented_by | default(counterparty.name) }}
{% if counterparty.type == 'company' %}Ametinimetus: {{ counterparty.represented_by_title }}{% endif %}
Kuupäev: ___________
Koht: ___________

---

## Lisa A: Amortisatsioonigraafik

{% if repayment_structure == 'amortizing' %}
[Bruno täidab selle laenu parameetritest arvutatud osamaksepõhise põhisumma/intressi jaotusega.]
{% else %}
Ei kohaldu (Laen ei ole amortiseeruv).
{% endif %}

---

<!--
LEPINGUMALLI LÕPP

Järgnevad jaotised ei ole lepingu osa — need on dokumentatsioon Bruno
arendajatele ja operaatoritele.
-->
