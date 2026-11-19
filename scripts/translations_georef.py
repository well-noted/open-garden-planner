"""German, Spanish and Russian targets for plan georeferencing (ADR-079)."""

_ORIGIN_NOTE = (
    "The rotation is the plan's north orientation: changing it here changes it "
    "everywhere (sun, shade, reports)."
)
_POINTS_HINT = (
    "One point sets only the position; two or more also set scale and rotation. "
    "X and Y are canvas coordinates in cm (Y points up)."
)
_REPLACE = (
    "The satellite image is north-up. Replace this plan's georeference and set its "
    "north orientation to 0 degrees?"
)
_GEOCODE_NOTE = (
    "Lets an AI assistant set the initial map from a street address. The address is "
    "sent to Google with your API key; the Geocoding API must be enabled for that "
    "key. Off by default."
)
_LINK = "Link the canvas to GPS coordinates: origin, scale, rotation, reference points"

DE = {
    "Commands": {
        "Set georeference": "Georeferenz setzen",
        "Set initial map": "Ausgangskarte setzen",
    },
    "GardenPlannerApp": {
        "Georeference...": "Georeferenz...",
        "Georeference updated": "Georeferenz aktualisiert",
        _LINK: "Zeichenfläche mit GPS-Koordinaten verknüpfen: Ursprung, Maßstab, Drehung, Referenzpunkte",
        "Replace georeference?": "Georeferenz ersetzen?",
        _REPLACE: (
            "Das Satellitenbild ist nach Norden ausgerichtet. Georeferenz dieses Plans "
            "ersetzen und die Nordausrichtung auf 0 Grad setzen?"
        ),
    },
    "GeoreferenceDialog": {
        "Georeference": "Georeferenz",
        "This plan is georeferenced": "Dieser Plan ist georeferenziert",
        "Canvas origin and orientation": "Ursprung und Ausrichtung der Zeichenfläche",
        "Latitude of the origin (0, 0):": "Breitengrad des Ursprungs (0, 0):",
        "Longitude of the origin (0, 0):": "Längengrad des Ursprungs (0, 0):",
        "Metres per canvas unit:": "Meter pro Zeichenflächeneinheit:",
        "True north, clockwise from plan-up:": "Wahrer Norden, im Uhrzeigersinn von Plan-oben:",
        _ORIGIN_NOTE: (
            "Die Drehung ist die Nordausrichtung des Plans: Eine Änderung hier gilt überall "
            "(Sonne, Schatten, Berichte)."
        ),
        "Reference points": "Referenzpunkte",
        "Name": "Name",
        "Latitude": "Breitengrad",
        "Longitude": "Längengrad",
        "X (cm)": "X (cm)",
        "Y (cm)": "Y (cm)",
        "Residual (m)": "Restfehler (m)",
        "Add point": "Punkt hinzufügen",
        "Remove selected": "Auswahl entfernen",
        "Fit from reference points": "Aus Referenzpunkten berechnen",
        _POINTS_HINT: (
            "Ein Punkt legt nur die Position fest; zwei oder mehr legen auch Maßstab und "
            "Drehung fest. X und Y sind Zeichenflächenkoordinaten in cm (Y zeigt nach oben)."
        ),
        "Cannot fit: {reason}": "Berechnung nicht möglich: {reason}",
        "Fit of {n} point(s): RMS {rms:.3f} m": "Berechnung aus {n} Punkt(en): RMS {rms:.3f} m",
        "Last fit: RMS {rms:.3f} m": "Letzte Berechnung: RMS {rms:.3f} m",
    },
    "PreferencesDialog": {
        "Allow street-address lookup (Google Geocoding API)": (
            "Adresssuche erlauben (Google Geocoding API)"
        ),
        _GEOCODE_NOTE: (
            "Erlaubt einem KI-Assistenten, die Ausgangskarte anhand einer Straßenadresse zu "
            "setzen. Die Adresse wird mit Ihrem API-Schlüssel an Google gesendet; die "
            "Geocoding API muss für diesen Schlüssel aktiviert sein. Standardmäßig aus."
        ),
    },
}

ES = {
    "Commands": {
        "Set georeference": "Establecer georreferencia",
        "Set initial map": "Establecer mapa inicial",
    },
    "GardenPlannerApp": {
        "Georeference...": "Georreferencia...",
        "Georeference updated": "Georreferencia actualizada",
        _LINK: "Vincular el lienzo con coordenadas GPS: origen, escala, rotación, puntos de referencia",
        "Replace georeference?": "¿Reemplazar la georreferencia?",
        _REPLACE: (
            "La imagen satelital está orientada al norte. ¿Reemplazar la georreferencia de "
            "este plano y establecer su orientación norte en 0 grados?"
        ),
    },
    "GeoreferenceDialog": {
        "Georeference": "Georreferencia",
        "This plan is georeferenced": "Este plano está georreferenciado",
        "Canvas origin and orientation": "Origen y orientación del lienzo",
        "Latitude of the origin (0, 0):": "Latitud del origen (0, 0):",
        "Longitude of the origin (0, 0):": "Longitud del origen (0, 0):",
        "Metres per canvas unit:": "Metros por unidad del lienzo:",
        "True north, clockwise from plan-up:": "Norte verdadero, en sentido horario desde la parte superior del plano:",
        _ORIGIN_NOTE: (
            "La rotación es la orientación norte del plano: cambiarla aquí la cambia en todas "
            "partes (sol, sombra, informes)."
        ),
        "Reference points": "Puntos de referencia",
        "Name": "Nombre",
        "Latitude": "Latitud",
        "Longitude": "Longitud",
        "X (cm)": "X (cm)",
        "Y (cm)": "Y (cm)",
        "Residual (m)": "Residuo (m)",
        "Add point": "Añadir punto",
        "Remove selected": "Quitar selección",
        "Fit from reference points": "Ajustar con puntos de referencia",
        _POINTS_HINT: (
            "Un punto solo fija la posición; dos o más fijan también la escala y la rotación. "
            "X e Y son coordenadas del lienzo en cm (Y apunta hacia arriba)."
        ),
        "Cannot fit: {reason}": "No se puede ajustar: {reason}",
        "Fit of {n} point(s): RMS {rms:.3f} m": "Ajuste de {n} punto(s): RMS {rms:.3f} m",
        "Last fit: RMS {rms:.3f} m": "Último ajuste: RMS {rms:.3f} m",
    },
    "PreferencesDialog": {
        "Allow street-address lookup (Google Geocoding API)": (
            "Permitir la búsqueda de direcciones (API de Geocodificación de Google)"
        ),
        _GEOCODE_NOTE: (
            "Permite que un asistente de IA establezca el mapa inicial a partir de una "
            "dirección postal. La dirección se envía a Google con su clave de API; la API de "
            "Geocodificación debe estar habilitada para esa clave. Desactivado de forma "
            "predeterminada."
        ),
    },
}

RU = {
    "Commands": {
        "Set georeference": "Задать геопривязку",
        "Set initial map": "Задать начальную карту",
    },
    "GardenPlannerApp": {
        "Georeference...": "Геопривязка...",
        "Georeference updated": "Геопривязка обновлена",
        _LINK: "Связать холст с GPS-координатами: начало координат, масштаб, поворот, опорные точки",
        "Replace georeference?": "Заменить геопривязку?",
        _REPLACE: (
            "Спутниковый снимок ориентирован на север. Заменить геопривязку этого плана и "
            "установить ориентацию севера на 0 градусов?"
        ),
    },
    "GeoreferenceDialog": {
        "Georeference": "Геопривязка",
        "This plan is georeferenced": "План имеет геопривязку",
        "Canvas origin and orientation": "Начало координат и ориентация холста",
        "Latitude of the origin (0, 0):": "Широта начала координат (0, 0):",
        "Longitude of the origin (0, 0):": "Долгота начала координат (0, 0):",
        "Metres per canvas unit:": "Метров на единицу холста:",
        "True north, clockwise from plan-up:": "Истинный север, по часовой стрелке от верха плана:",
        _ORIGIN_NOTE: (
            "Поворот — это ориентация севера плана: изменение здесь действует везде "
            "(солнце, тень, отчёты)."
        ),
        "Reference points": "Опорные точки",
        "Name": "Название",
        "Latitude": "Широта",
        "Longitude": "Долгота",
        "X (cm)": "X (см)",
        "Y (cm)": "Y (см)",
        "Residual (m)": "Невязка (м)",
        "Add point": "Добавить точку",
        "Remove selected": "Удалить выбранные",
        "Fit from reference points": "Рассчитать по опорным точкам",
        _POINTS_HINT: (
            "Одна точка задаёт только положение; две и более задают также масштаб и поворот. "
            "X и Y — координаты холста в см (Y направлена вверх)."
        ),
        "Cannot fit: {reason}": "Не удаётся рассчитать: {reason}",
        "Fit of {n} point(s): RMS {rms:.3f} m": "Расчёт по {n} точкам: СКО {rms:.3f} м",
        "Last fit: RMS {rms:.3f} m": "Последний расчёт: СКО {rms:.3f} м",
    },
    "PreferencesDialog": {
        "Allow street-address lookup (Google Geocoding API)": (
            "Разрешить поиск по адресу (Google Geocoding API)"
        ),
        _GEOCODE_NOTE: (
            "Позволяет ИИ-ассистенту задать начальную карту по адресу. Адрес отправляется в "
            "Google вместе с вашим ключом API; для этого ключа должен быть включён Geocoding "
            "API. По умолчанию отключено."
        ),
    },
}
