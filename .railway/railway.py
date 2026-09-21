from railway_sdk import define_railway, github, project, service, volume

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
        env={"DATABASE_PATH": "/data/ildolomiti.db"},
        replicas={REGION: 1},
        volumeMounts={"/data": volume("data", sizeMB=512)},
    )

    return project("ildolomiti-telegram", resources=[bot])
