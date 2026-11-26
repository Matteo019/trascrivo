> ⚠️ **BOZZA — NON PUBBLICARE COSÌ COM'È.**
> Questo testo è una bozza scritta per descrivere come funziona tecnicamente il servizio. Deve essere
> **controllato e completato da una persona competente** (consulente privacy o legale) prima della
> pubblicazione. Non afferma e non garantisce la conformità al GDPR o ad altre norme.
> Le parti tra [parentesi quadre] vanno completate.

# Informativa sul trattamento dei dati personali — Trascrizioni

Ultimo aggiornamento: [data]

## 1. Chi tratta i dati

**Titolare del trattamento:** [nome e cognome / ragione sociale], [indirizzo], contatto: [email dedicata].

Il servizio è gestito da una persona privata, su un server di sua proprietà, ed è accessibile **solo su
invito**. [Da valutare con il consulente: se e in che misura il servizio rientri nell'uso personale o
domestico, e il ruolo di chi lo usa rispetto alle persone registrate.]

## 2. Cosa fa il servizio

Permette agli utenti invitati di caricare un file audio, o di registrarlo dal browser, e di ottenerne la
trascrizione in testo. La trascrizione avviene **sul server del titolare**, con un modello di riconoscimento
vocale eseguito in locale (Whisper). Audio e testo **non vengono inviati a servizi di trascrizione o di
intelligenza artificiale di terzi**.

## 3. Quali dati trattiamo

| Dato | Perché | Per quanto tempo |
|---|---|---|
| Email, nome, password (salvata solo come hash Argon2) | Creare e proteggere l'account | Finché l'account esiste [definire la cancellazione degli account inattivi] |
| File audio caricati o registrati | Fare la trascrizione | **Cancellati appena la trascrizione è terminata**, anche in caso di errore o annullamento |
| Testo della trascrizione | Permettere all'utente di scaricarlo | **[7] giorni**, oppure meno se l'utente lo elimina prima con il pulsante "Elimina" |
| Dati del job (stato, date, durata dell'audio, lingua, progetto, eventuale errore tecnico) | Gestire la coda e l'assistenza | Come il testo; i job falliti o annullati per [7] giorni |
| Glossario dei progetti (parole scritte dall'utente) | Migliorare il riconoscimento dei termini tecnici | Finché l'utente non elimina il progetto |
| Cookie tecnico di sessione | Tenere l'utente collegato | Fino all'uscita o a [7] giorni |
| Impronte (hash) di indirizzo IP ed email dei tentativi di accesso falliti | Proteggere gli account da tentativi ripetuti | 24 ore |

Il nome originale dei file caricati **non viene salvato**. I log tecnici del servizio contengono solo
identificativi dei job, mai il testo trascritto, i nomi dei file o le email.

Non usiamo cookie di profilazione, strumenti di analisi o pubblicità. I dati non vengono usati per
addestrare modelli né per altri scopi.

## 4. Dati di altre persone contenuti nell'audio

Le registrazioni possono contenere la voce e le parole di altre persone (per esempio docenti o colleghi).
**L'utente che carica l'audio deve aver informato queste persone** e, dove necessario, averne ottenuto il
consenso o avere un'altra base giuridica valida. [Da approfondire con il consulente: ruoli e responsabilità
dell'utente rispetto ai terzi registrati.]

## 5. Base giuridica

[Da definire con il consulente: ad esempio esecuzione del servizio richiesto dall'utente (art. 6.1.b GDPR)
per account e trascrizioni; legittimo interesse alla sicurezza (art. 6.1.f) per la protezione degli accessi.]

## 6. Chi altro vede i dati

- **Cloudflare, Inc.** fornisce il collegamento sicuro (HTTPS) tra internet e il server. Il traffico, file
  audio compresi, **transita cifrato attraverso la rete di Cloudflare**, che ne termina la cifratura TLS
  prima di inoltrarlo al server. Cloudflare non conserva i contenuti come parte del servizio, ma è un
  fornitore che tratta i dati in transito. [Verificare con il consulente le condizioni contrattuali (DPA) e
  i trasferimenti extra UE.]
- **L'amministratore del servizio** vede l'elenco degli utenti e lo stato dei job (data, durata, esito), ma
  l'interfaccia **non gli mostra il testo** delle trascrizioni degli altri utenti. Ha accesso tecnico al
  server e quindi, in linea di principio, ai dati memorizzati. [Descrivere le misure organizzative adottate.]
- Nessun altro destinatario. I dati non vengono venduti né ceduti.

## 7. Dove sono i dati

Su un server fisico del titolare in Italia ([città]), in un database PostgreSQL e in una cartella
temporanea per l'audio. [Indicare le misure di sicurezza: cifratura del disco, backup e loro conservazione,
aggiornamenti.]

## 8. I tuoi diritti

Puoi chiedere accesso, rettifica, cancellazione, limitazione, portabilità e opporti al trattamento,
scrivendo a [email]. Puoi cancellare in autonomia una trascrizione con il pulsante "Elimina". Hai diritto
di proporre reclamo al Garante per la protezione dei dati personali (www.garanteprivacy.it).

## 9. Avvertenze

- Se copi una trascrizione in un servizio esterno (per esempio un'intelligenza artificiale online), i dati
  escono dal controllo di questo servizio e valgono le condizioni di quel servizio.
- La trascrizione automatica può contenere errori.

## 10. Modifiche

[Come vengono comunicate le modifiche a questa informativa.]
