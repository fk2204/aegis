"""
aegis_ui — the redesigned AEGIS UI, packaged so it can drop into a src/ layout
without assuming the rest of the app's package name.

Mount it from your FastAPI entrypoint with:

    from aegis_ui.router import router as aegis_v2_router
    app.include_router(aegis_v2_router)

Everything it serves lives under /v2, so existing routes are untouched.
"""
