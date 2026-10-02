import json
from hashlib import sha256
from html import escape


def webhook_setup_text(url: str) -> str:
    """Render the URL once, inside a copyable Home Assistant configuration example."""
    command = f"svitlo_{sha256(url.encode()).hexdigest()[:16]}"
    example = f"""rest_command:
  {command}:
    url: {json.dumps(url)}
    method: POST
    content_type: "application/json"
    timeout: 10
    payload: >-
      {{"state": "{{{{ states('binary_sensor.power') }}}}"}}

automation:
  - alias: "Передавати стан електроенергії"
    triggers:
      - trigger: state
        entity_id: binary_sensor.power
        to:
          - "on"
          - "off"
      - trigger: homeassistant
        event: start
      - trigger: time_pattern
        minutes: "/1"
    conditions:
      - condition: template
        value_template: "{{{{ states('binary_sensor.power') in ['on', 'off'] }}}}"
    actions:
      - action: rest_command.{command}
    mode: queued
    max: 10"""
    return (
        "Не публікуйте це посилання — воно використовується для передачі стану вашого пристрою.\n\n"
        "Посилання показано лише один раз у прикладі нижче. Збережіть його.\n\n"
        "Замініть binary_sensor.power на вашу сутність: on має означати, що світло є, "
        "off — що світла немає. Додайте приклад до configuration.yaml. "
        "Якщо розділи rest_command або automation вже існують, додайте записи до них.\n\n"
        f"<pre>{escape(example)}</pre>\n\n"
        "Перевірте конфігурацію та перезапустіть Home Assistant. "
        "Приклад надсилає стан після змін, запуску та щохвилини. "
        "Невідомий стан не надсилається.\n"
        "Після отримання стану натисніть «🧪 Перевірити підключення», потім збережіть пристрій."
    )
