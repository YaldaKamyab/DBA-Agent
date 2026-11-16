"""Tiny console abstraction handed to interactive stages (easy to mock)."""


class UI:
    def __init__(self, input_fn=input, out=print):
        self._input = input_fn
        self.out = out

    def input(self):
        """Read one line. EOFError / KeyboardInterrupt propagate (= pause)."""
        return self._input()

    def confirm(self, question):
        self.out(f"{question} [Y/n]")
        try:
            return self._input().strip().lower() in ("", "y", "yes")
        except EOFError:
            return False
