try:
    from .gitmsg import main
except ImportError:  # 直接 python __main__.py 运行时的 fallback
    from gitmsg import main

if __name__ == "__main__":
    raise SystemExit(main())
