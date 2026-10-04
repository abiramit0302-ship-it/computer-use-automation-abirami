"""Shared test setup. pytest loads this file automatically before any test module,
so every test sees the same environment no matter which tests are selected."""

from dotenv import load_dotenv

load_dotenv()