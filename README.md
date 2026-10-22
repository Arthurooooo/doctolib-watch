# doctolib-watch

Verifie toutes les ~10 minutes les disponibilites Doctolib des medecins configures
et envoie une notification push via ntfy quand un nouveau creneau apparait.

Configuration via les secrets du depot :
- `DOCTORS_JSON` : liste JSON de medecins `{"name", "booking_page", "motive"}`
- `NTFY_TOPIC` : topic ntfy auquel s'abonner dans l'app

Test manuel : onglet Actions > Doctolib watch > Run workflow.
