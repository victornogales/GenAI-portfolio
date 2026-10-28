
from anthropic.types import ToolParam
from datetime import datetime

def get_current_datetime(date_format="%Y-%m-%d %H:%M:%S"):
    if not date_format:
        raise ValueError("date_format cannot be empty")
    return datetime.now().strftime(date_format)


get_current_datetime_schema = {
  "name": "get_current_datetime",
  "description": "Returns the current local date and/or time of the machine running the tool, formatted as a string. Use it whenever the user asks for the current date, time, day of the week, month or year, or when a task needs today's date (for example, timestamping a note or working out how many days until an event). Do not use it for converting between time zones, parsing or doing arithmetic on other dates, or getting the date of past or future events. It only reports 'now'. The time is the server's local time with no time zone information, so time zone directives such as %Z and %z return an empty string. If date_format is omitted, the result looks like '2026-10-22 11:34:05'.",
  "input_schema": {
    "type": "object",
    "properties": {
      "date_format": {
        "type": "string",
        "minLength": 1,
        "description": "Optional Python strftime format string that controls the output. Defaults to '%Y-%m-%d %H:%M:%S' (e.g. '2026-10-22 11:34:05'). Common directives are %Y (4-digit year), %m (month 01-12), %d (day 01-31), %H (hour 00-23), %M (minute), %S (second), %A (weekday name) and %B (month name). Examples are '%H:%M' for '11:34', '%A' for 'Thursday', and '%d %B %Y' for '22 October 2026'. Must not be an empty string."
      }
    },
    "required": []
  },
  "input_examples": [
    {},
    { "date_format": "%H:%M" },
    { "date_format": "%A, %d %B %Y" }
  ]
}


# Default format: "2024-01-15 14:30:25"
print(get_current_datetime())

# Just hour and minute: "14:30"
print(get_current_datetime("%H:%M"))