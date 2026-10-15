class Script:
    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.calls: list = []

    async def __call__(self, request):
        self.calls.append(request)
        index = len(self.calls) - 1
        if index >= len(self.results):
            raise AssertionError(f"scripted worker called {len(self.calls)} times")
        return self.results[index]
