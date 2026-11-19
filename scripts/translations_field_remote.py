"""German, Spanish and Russian targets for the Field Remote preferences, pairing dialog and status (ADR-081).

Russian has no fill script: its targets are applied to the ``.ts`` file from ``RU`` when the
catalogs are updated, and kept here so the three languages stay together.
"""

_INTRO = (
    "Open this link in Chrome on your Android phone. Your phone's browser will warn "
    "that the certificate is not trusted; that is expected for a certificate this "
    "computer made itself. Compare the fingerprint below with the one the browser "
    "shows (under the warning's certificate details), then continue."
)
_WARNING = (
    "Anyone on this network who gets the pairing link can read your plan. Use a "
    "network you trust, and rotate the token if the link leaks."
)
_ENABLE_TIP = (
    "Lets a phone on your network open a page served by this computer. Off by "
    "default. It uses its own port and access token, separate from the Agent API."
)
_RECORDS_TIP = (
    "Lets the paired phone add pending field records (notes, photos, positions). "
    "The phone never edits the plan; you or an AI assistant approve records later. "
    "Off by default."
)
_BIND_TIP = (
    "The one address the server listens on. 127.0.0.1 reaches this computer only. "
    "Choose a local-network or Tailscale address to reach it from a phone."
)
_LINK_NOTE = "The link contains the access token. It is hidden after two minutes; do not share it."
_NOT_RUNNING = "Field Remote is not running. Click Save, then open this again."
_ROTATE_TIP = "Replace the token now; every paired phone must pair again."

DE = {
    "FieldRemoteDialog": {
        "Pair a phone": "Smartphone koppeln",
        _INTRO: (
            "Öffnen Sie diesen Link in Chrome auf Ihrem Android-Smartphone. Der Browser "
            "warnt, dass dem Zertifikat nicht vertraut wird; das ist bei einem Zertifikat, "
            "das dieser Computer selbst erstellt hat, zu erwarten. Vergleichen Sie den "
            "Fingerabdruck unten mit dem, den der Browser anzeigt (in den Zertifikatsdetails "
            "der Warnung), und fahren Sie dann fort."
        ),
        "Certificate fingerprint (SHA-256):": "Zertifikat-Fingerabdruck (SHA-256):",
        _LINK_NOTE: (
            "Der Link enthält das Zugriffstoken. Er wird nach zwei Minuten ausgeblendet; "
            "geben Sie ihn nicht weiter."
        ),
        "Copy link": "Link kopieren",
        "Close": "Schließen",
        "Hidden. Close this window and open it again.": "Ausgeblendet. Fenster schließen und erneut öffnen.",
    },
    "FieldRemoteHost": {
        "Port {port} is already in use": "Port {port} wird bereits verwendet",
        "Field Remote failed to start (see log)": "Field Remote konnte nicht gestartet werden (siehe Protokoll)",
    },
    "GardenPlannerApp": {
        "Field Remote running at {url}": "Field Remote läuft unter {url}",
        "Field Remote: {reason}": "Field Remote: {reason}",
    },
    "PreferencesDialog": {
        "Field Remote (phone)": "Field Remote (Smartphone)",
        "Field Remote": "Field Remote",
        "Enable Field Remote (HTTPS server for a phone)": "Field Remote aktivieren (HTTPS-Server für ein Smartphone)",
        _ENABLE_TIP: (
            "Ermöglicht einem Smartphone in Ihrem Netzwerk, eine von diesem Computer "
            "bereitgestellte Seite zu öffnen. Standardmäßig aus. Es nutzt einen eigenen "
            "Port und ein eigenes Zugriffstoken, getrennt von der Agent API."
        ),
        "Address:": "Adresse:",
        _BIND_TIP: (
            "Die eine Adresse, auf der der Server lauscht. 127.0.0.1 erreicht nur diesen "
            "Computer. Wählen Sie eine Adresse im lokalen Netz oder eine Tailscale-Adresse, "
            "um ihn vom Smartphone aus zu erreichen."
        ),
        _WARNING: (
            "Jeder in diesem Netzwerk, der den Kopplungslink erhält, kann Ihren Plan lesen. "
            "Nutzen Sie ein vertrauenswürdiges Netzwerk und erneuern Sie das Token, falls "
            "der Link bekannt wird."
        ),
        "Allow phone to submit field records": "Smartphone darf Feldaufzeichnungen übermitteln",
        _RECORDS_TIP: (
            "Erlaubt dem gekoppelten Smartphone, ausstehende Feldaufzeichnungen "
            "(Notizen, Fotos, Positionen) hinzuzufügen. Das Smartphone ändert den Plan nie; "
            "Sie oder ein KI-Assistent genehmigen die Aufzeichnungen später. Standardmäßig aus."
        ),
        "Show pairing link…": "Kopplungslink anzeigen…",
        "Rotate token": "Token erneuern",
        _ROTATE_TIP: "Ersetzt das Token sofort; jedes gekoppelte Smartphone muss neu gekoppelt werden.",
        _NOT_RUNNING: "Field Remote läuft nicht. Auf Speichern klicken und dies dann erneut öffnen.",
    },
}

ES = {
    "FieldRemoteDialog": {
        "Pair a phone": "Vincular un teléfono",
        _INTRO: (
            "Abra este enlace en Chrome en su teléfono Android. El navegador avisará de que "
            "el certificado no es de confianza; es lo esperado en un certificado que este "
            "ordenador ha creado por sí mismo. Compare la huella de abajo con la que muestra "
            "el navegador (en los detalles del certificado de la advertencia) y continúe."
        ),
        "Certificate fingerprint (SHA-256):": "Huella del certificado (SHA-256):",
        _LINK_NOTE: (
            "El enlace contiene el token de acceso. Se oculta tras dos minutos; no lo comparta."
        ),
        "Copy link": "Copiar enlace",
        "Close": "Cerrar",
        "Hidden. Close this window and open it again.": "Oculto. Cierre esta ventana y ábrala de nuevo.",
    },
    "FieldRemoteHost": {
        "Port {port} is already in use": "El puerto {port} ya está en uso",
        "Field Remote failed to start (see log)": "Field Remote no pudo iniciarse (consulte el registro)",
    },
    "GardenPlannerApp": {
        "Field Remote running at {url}": "Field Remote en funcionamiento en {url}",
        "Field Remote: {reason}": "Field Remote: {reason}",
    },
    "PreferencesDialog": {
        "Field Remote (phone)": "Field Remote (teléfono)",
        "Field Remote": "Field Remote",
        "Enable Field Remote (HTTPS server for a phone)": "Activar Field Remote (servidor HTTPS para un teléfono)",
        _ENABLE_TIP: (
            "Permite que un teléfono de su red abra una página servida por este ordenador. "
            "Desactivado por defecto. Usa su propio puerto y token de acceso, separados de "
            "la Agent API."
        ),
        "Address:": "Dirección:",
        _BIND_TIP: (
            "La única dirección en la que escucha el servidor. 127.0.0.1 solo llega a este "
            "ordenador. Elija una dirección de la red local o de Tailscale para acceder "
            "desde un teléfono."
        ),
        _WARNING: (
            "Cualquiera en esta red que obtenga el enlace de vinculación puede leer su plan. "
            "Use una red de confianza y renueve el token si el enlace se filtra."
        ),
        "Allow phone to submit field records": "Permitir que el teléfono envíe registros de campo",
        _RECORDS_TIP: (
            "Permite que el teléfono vinculado añada registros de campo pendientes (notas, "
            "fotos, posiciones). El teléfono nunca edita el plan; usted o un asistente de IA "
            "aprueban los registros más tarde. Desactivado por defecto."
        ),
        "Show pairing link…": "Mostrar enlace de vinculación…",
        "Rotate token": "Renovar token",
        _ROTATE_TIP: "Sustituye el token ahora; cada teléfono vinculado deberá vincularse de nuevo.",
        _NOT_RUNNING: "Field Remote no está en funcionamiento. Haga clic en Guardar y vuelva a abrir esto.",
    },
}

RU = {
    "FieldRemoteDialog": {
        "Pair a phone": "Связать телефон",
        _INTRO: (
            "Откройте эту ссылку в Chrome на телефоне Android. Браузер предупредит, что "
            "сертификату нельзя доверять; для сертификата, созданного самим компьютером, "
            "это ожидаемо. Сравните отпечаток ниже с тем, что показывает браузер (в "
            "сведениях о сертификате в предупреждении), и продолжайте."
        ),
        "Certificate fingerprint (SHA-256):": "Отпечаток сертификата (SHA-256):",
        _LINK_NOTE: (
            "Ссылка содержит токен доступа. Через две минуты она скрывается; не передавайте её."
        ),
        "Copy link": "Копировать ссылку",
        "Close": "Закрыть",
        "Hidden. Close this window and open it again.": "Скрыто. Закройте это окно и откройте снова.",
    },
    "FieldRemoteHost": {
        "Port {port} is already in use": "Порт {port} уже используется",
        "Field Remote failed to start (see log)": "Не удалось запустить Field Remote (см. журнал)",
    },
    "GardenPlannerApp": {
        "Field Remote running at {url}": "Field Remote работает по адресу {url}",
        "Field Remote: {reason}": "Field Remote: {reason}",
    },
    "PreferencesDialog": {
        "Field Remote (phone)": "Field Remote (телефон)",
        "Field Remote": "Field Remote",
        "Enable Field Remote (HTTPS server for a phone)": "Включить Field Remote (HTTPS-сервер для телефона)",
        _ENABLE_TIP: (
            "Позволяет телефону в вашей сети открыть страницу, которую отдаёт этот компьютер. "
            "По умолчанию отключено. Использует свой порт и свой токен доступа, отдельные от "
            "Agent API."
        ),
        "Address:": "Адрес:",
        _BIND_TIP: (
            "Единственный адрес, который слушает сервер. 127.0.0.1 доступен только на этом "
            "компьютере. Выберите адрес локальной сети или Tailscale, чтобы подключаться с "
            "телефона."
        ),
        _WARNING: (
            "Любой в этой сети, кто получит ссылку для связывания, сможет прочитать ваш план. "
            "Используйте надёжную сеть и обновите токен, если ссылка стала известна."
        ),
        "Allow phone to submit field records": "Разрешить телефону отправлять полевые записи",
        _RECORDS_TIP: (
            "Позволяет связанному телефону добавлять ожидающие полевые записи (заметки, фото, "
            "позиции). Телефон никогда не меняет план; вы или ИИ-ассистент подтверждаете "
            "записи позже. По умолчанию отключено."
        ),
        "Show pairing link…": "Показать ссылку для связывания…",
        "Rotate token": "Обновить токен",
        _ROTATE_TIP: "Немедленно заменяет токен; каждый связанный телефон нужно связать заново.",
        _NOT_RUNNING: "Field Remote не запущен. Нажмите «Сохранить», затем откройте это снова.",
    },
}

# -- security review additions: certificate controls and settings validation --
_FP_TIP = "SHA-256 fingerprint of this computer's certificate. It is not a secret."
_REGEN_TIP = "Create a new certificate and key now; phones must accept the new certificate again."
_BIND_BAD = "The Field Remote address cannot be used: {reason}"
_PORT_SHARED = "Field Remote and the Agent API cannot share port {port}."
_AGENT_PORT = "Port {port} is used by the Agent API"

DE["PreferencesDialog"].update({
    "Certificate fingerprint:": "Zertifikat-Fingerabdruck:",
    _FP_TIP: "SHA-256-Fingerabdruck des Zertifikats dieses Computers. Er ist kein Geheimnis.",
    "Regenerate certificate": "Zertifikat neu erstellen",
    _REGEN_TIP: "Erstellt sofort ein neues Zertifikat samt Schlüssel; Smartphones müssen das neue Zertifikat erneut akzeptieren.",
    "Not created yet": "Noch nicht erstellt",
    _BIND_BAD: "Die Field-Remote-Adresse kann nicht verwendet werden: {reason}",
    _PORT_SHARED: "Field Remote und die Agent API können nicht denselben Port {port} nutzen.",
})
DE["FieldRemoteHost"][_AGENT_PORT] = "Port {port} wird von der Agent API verwendet"

ES["PreferencesDialog"].update({
    "Certificate fingerprint:": "Huella del certificado:",
    _FP_TIP: "Huella SHA-256 del certificado de este ordenador. No es un secreto.",
    "Regenerate certificate": "Regenerar certificado",
    _REGEN_TIP: "Crea ahora un nuevo certificado y clave; los teléfonos deberán aceptar de nuevo el certificado.",
    "Not created yet": "Aún no creado",
    _BIND_BAD: "No se puede usar la dirección de Field Remote: {reason}",
    _PORT_SHARED: "Field Remote y la Agent API no pueden compartir el puerto {port}.",
})
ES["FieldRemoteHost"][_AGENT_PORT] = "El puerto {port} lo usa la Agent API"

RU["PreferencesDialog"].update({
    "Certificate fingerprint:": "Отпечаток сертификата:",
    _FP_TIP: "Отпечаток SHA-256 сертификата этого компьютера. Это не секрет.",
    "Regenerate certificate": "Создать сертификат заново",
    _REGEN_TIP: "Сразу создаёт новый сертификат и ключ; телефонам придётся снова принять новый сертификат.",
    "Not created yet": "Ещё не создан",
    _BIND_BAD: "Адрес Field Remote использовать нельзя: {reason}",
    _PORT_SHARED: "Field Remote и Agent API не могут использовать один порт {port}.",
})
RU["FieldRemoteHost"][_AGENT_PORT] = "Порт {port} используется Agent API"

# View menu entry for the phone's position overlay (WP-C).
_PHONE_POSITION = "Show &Phone Position"
_PHONE_POSITION_TIP = "Show the position a paired phone reports (not saved with the plan)"
DE["GardenPlannerApp"].update({
    _PHONE_POSITION: "&Smartphone-Position anzeigen",
    _PHONE_POSITION_TIP: "Die vom gekoppelten Smartphone gemeldete Position anzeigen (wird nicht im Plan gespeichert)",
})
ES["GardenPlannerApp"].update({
    _PHONE_POSITION: "Mostrar posición del &teléfono",
    _PHONE_POSITION_TIP: "Mostrar la posición que informa un teléfono vinculado (no se guarda en el plan)",
})
RU["GardenPlannerApp"].update({
    _PHONE_POSITION: "Показывать &положение телефона",
    _PHONE_POSITION_TIP: "Показывать положение, которое сообщает подключённый телефон (не сохраняется в плане)",
})
