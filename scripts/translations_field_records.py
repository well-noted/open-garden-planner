"""German, Spanish and Russian targets for the Field records review panel (WP-R, ADR-080 addendum).

Russian has no fill script: its targets are applied to the ``.ts`` file from ``RU`` when the
catalogs are updated, and kept here so the three languages stay together.
"""

_SAVE = "Save the plan first: field records are stored beside the saved file."
_FIRST = "Review field records and place them on the plan"
_PANEL_KEYS = (
    "Accuracy: {m} m",
    "All kinds",
    "All statuses",
    "Approved",
    "Bearing: {deg}°",
    "Field records",
    "Fix: {mode}",
    "Irrigation line",
    "Kind",
    "Kind:",
    "Measurement",
    "Name",
    "Note",
    "Notes",
    "Pending",
    "Placed",
    "Placement request",
    "Placement request: {what}",
    "Plant",
    "Precision: {hint}",
    "Record",
    "Reference point",
    "Refresh",
    "Reject all",
    "Rejected",
    "Reopen group",
    "Requested by",
    _SAVE,
    "Select a record to review it.",
    "Status",
    "Status:",
    "Superseded",
    "The placed object is no longer on the plan.",
    "The reference point was not applied.",
    "Wanted",
    "{n} candidate(s)",
)

_DE_PANEL = (
    "Genauigkeit: {m} m",
    "Alle Arten",
    "Alle Status",
    "Genehmigt",
    "Peilung: {deg}°",
    "Feldaufzeichnungen",
    "Positionsmodus: {mode}",
    "Bewässerungsleitung",
    "Art",
    "Art:",
    "Messung",
    "Name",
    "Notiz",
    "Notizen",
    "Ausstehend",
    "Platziert",
    "Platzierungswunsch",
    "Platzierungswunsch: {what}",
    "Pflanze",
    "Genauigkeitshinweis: {hint}",
    "Aufzeichnung",
    "Referenzpunkt",
    "Aktualisieren",
    "Alle ablehnen",
    "Abgelehnt",
    "Gruppe wieder öffnen",
    "Angefragt von",
    "Speichern Sie zuerst den Plan: Feldaufzeichnungen liegen neben der gespeicherten Datei.",
    "Wählen Sie eine Aufzeichnung zur Prüfung aus.",
    "Status",
    "Status:",
    "Ersetzt",
    "Das platzierte Objekt ist nicht mehr im Plan.",
    "Der Referenzpunkt wurde nicht übernommen.",
    "Gewünscht",
    "{n} Kandidat(en)",
)
_ES_PANEL = (
    "Precisión: {m} m",
    "Todos los tipos",
    "Todos los estados",
    "Aprobado",
    "Rumbo: {deg}°",
    "Registros de campo",
    "Modo de posición: {mode}",
    "Línea de riego",
    "Tipo",
    "Tipo:",
    "Medición",
    "Nombre",
    "Nota",
    "Notas",
    "Pendiente",
    "Colocado",
    "Solicitud de colocación",
    "Solicitud de colocación: {what}",
    "Planta",
    "Indicación de precisión: {hint}",
    "Registro",
    "Punto de referencia",
    "Actualizar",
    "Rechazar todo",
    "Rechazado",
    "Reabrir grupo",
    "Solicitado por",
    "Guarde primero el plan: los registros de campo se guardan junto al archivo guardado.",
    "Seleccione un registro para revisarlo.",
    "Estado",
    "Estado:",
    "Sustituido",
    "El objeto colocado ya no está en el plan.",
    "El punto de referencia no se aplicó.",
    "Deseado",
    "{n} candidato(s)",
)
_RU_PANEL = (
    "Точность: {m} м",
    "Все виды",
    "Все статусы",
    "Одобрено",
    "Азимут: {deg}°",
    "Полевые записи",
    "Режим позиции: {mode}",
    "Линия полива",
    "Вид",
    "Вид:",
    "Измерение",
    "Название",
    "Заметка",
    "Заметки",
    "Ожидает",
    "Размещено",
    "Запрос на размещение",
    "Запрос на размещение: {what}",
    "Растение",
    "Подсказка о точности: {hint}",
    "Запись",
    "Опорная точка",
    "Обновить",
    "Отклонить все",
    "Отклонено",
    "Открыть группу снова",
    "Запросил",
    "Сначала сохраните план: полевые записи хранятся рядом с сохранённым файлом.",
    "Выберите запись для проверки.",
    "Статус",
    "Статус:",
    "Заменено",
    "Размещённого объекта больше нет на плане.",
    "Опорная точка не применена.",
    "Желаемое",
    "Кандидатов: {n}",
)

_DE_REST = {
    "FieldRecordApprover": {"Place irrigation line": "Bewässerungsleitung platzieren"},
    "FieldRecordsDialog": {"Field records": "Feldaufzeichnungen"},
    "GardenPlannerApp": {
        "Field Records...": "Feldaufzeichnungen...",
        _FIRST: "Feldaufzeichnungen prüfen und im Plan platzieren",
    },
    "GeoreferenceDialog": {
        "Field point added; add a second point to fit.": (
            "Feldpunkt hinzugefügt; fügen Sie einen zweiten Punkt für die Anpassung hinzu."
        )
    },
    "_MapPreview": {"No position on the plan": "Keine Position im Plan"},
    "_RecordCard": {
        "Approve": "Genehmigen",
        "Approve this one": "Diesen genehmigen",
        "No photo": "Kein Foto",
        "Reject": "Ablehnen",
        "Reopen": "Wieder öffnen",
    },
}
_ES_REST = {
    "FieldRecordApprover": {"Place irrigation line": "Colocar línea de riego"},
    "FieldRecordsDialog": {"Field records": "Registros de campo"},
    "GardenPlannerApp": {
        "Field Records...": "Registros de campo...",
        _FIRST: "Revisar los registros de campo y colocarlos en el plan",
    },
    "GeoreferenceDialog": {
        "Field point added; add a second point to fit.": (
            "Punto de campo añadido; añada un segundo punto para ajustar."
        )
    },
    "_MapPreview": {"No position on the plan": "Sin posición en el plan"},
    "_RecordCard": {
        "Approve": "Aprobar",
        "Approve this one": "Aprobar este",
        "No photo": "Sin foto",
        "Reject": "Rechazar",
        "Reopen": "Reabrir",
    },
}
_RU_REST = {
    "FieldRecordApprover": {"Place irrigation line": "Разместить линию полива"},
    "FieldRecordsDialog": {"Field records": "Полевые записи"},
    "GardenPlannerApp": {
        "Field Records...": "Полевые записи...",
        _FIRST: "Проверить полевые записи и разместить их на плане",
    },
    "GeoreferenceDialog": {
        "Field point added; add a second point to fit.": (
            "Полевая точка добавлена; добавьте вторую точку для подгонки."
        )
    },
    "_MapPreview": {"No position on the plan": "Нет позиции на плане"},
    "_RecordCard": {
        "Approve": "Одобрить",
        "Approve this one": "Одобрить эту",
        "No photo": "Нет фото",
        "Reject": "Отклонить",
        "Reopen": "Открыть снова",
    },
}


def _build(panel: tuple[str, ...], rest: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    assert len(panel) == len(_PANEL_KEYS)
    return {**rest, "FieldRecordsPanel": dict(zip(_PANEL_KEYS, panel, strict=True))}


DE = _build(_DE_PANEL, _DE_REST)
ES = _build(_ES_PANEL, _ES_REST)
RU = _build(_RU_PANEL, _RU_REST)
