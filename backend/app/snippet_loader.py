import importlib
import logging
import pkgutil


def load_snippets(app) -> None:
    loaded, failed = [], []

    try:
        from . import snippets as snippets_pkg  # requiere backend/app/snippets/__init__.py
        pkg_prefix = f"{snippets_pkg.__name__}."
        for m in pkgutil.walk_packages(snippets_pkg.__path__, prefix=pkg_prefix):
            modname = m.name
            relname = modname[len(pkg_prefix):]
            if "." in relname:
                leaf = relname.split(".")[-1]
                if leaf not in ("router", "routes"):
                    continue
            elif m.ispkg:
                continue
            try:
                mod = importlib.import_module(modname)
                if getattr(mod, "ENABLED", True) and hasattr(mod, "router"):
                    router_prefix = getattr(mod, "ROUTER_PREFIX", "/api/v1")
                    app.include_router(mod.router, prefix=router_prefix)
                    loaded.append(relname)
                else:
                    failed.append((relname, "sin 'router' o deshabilitado"))
            except Exception as exc:
                failed.append((relname, f"import error: {exc}"))
    except Exception as exc:
        failed.append(("__snippets__", f"package error: {exc}"))

    log = logging.getLogger("uvicorn")
    if loaded:
        log.info(f"Snippets cargados: {', '.join(loaded)}")
    if failed:
        for name, reason in failed:
            log.warning(f"Snippet omitido: {name} → {reason}")
