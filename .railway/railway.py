from railway_sdk import define_railway, github, preserve, project, service, volume

REGION = "europe-west4-drams3a"


@define_railway
def main(ctx=None):
    bot = service(
        "ildolomiti-telegram",
        source=github(
            "matteocontrini/ildolomiti-telegram",
            branch="main",
            checkSuites=True,
        ),
        start="python main.py",
        deploy={
            "limitOverride": {
                "containers": {
                    "cpu": 1,
                    "memoryBytes": 1000 * 1000 * 1000,
                },
            },
        },
        env={
            "DATABASE_PATH": "/data/ildolomiti.db",
            "BOT_TOKEN": preserve(),
            "OPENROUTER_API_KEY": preserve(),
            "TELEGRAM_CHANNEL": preserve(),
        },
        replicas={REGION: 1},
        volumeMounts={
            "/data": volume("data", region=REGION, sizeMB=512),
        },
    )

    return project("ildolomiti-telegram", resources=[bot])
