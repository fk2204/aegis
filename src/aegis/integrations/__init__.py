"""Third-party integrations that AEGIS pulls FROM (not Bedrock / Supabase /
Close — those have their own first-class modules).

Currently:
  * ``google_drive`` — funder guidelines PDF sync. Reads a Drive folder
    of funder subfolders + their latest PDF, downloads, hands off to
    the existing Bedrock guidelines extractor.
"""
