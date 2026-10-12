EXTRACT_PROMPT = """Extract the requested fields from this insurance document.
Return one JSON object. Dates are YYYY-MM-DD. Totals are numbers.
line_items is a list of objects with description and amount.
Use null for a field you cannot see. Do not invent a name, date, or amount.
"""
