"""Command-line interface for configuration management.

Provides interactive menus for viewing and editing configuration
without requiring code changes.
"""

import sys
from pathlib import Path
from typing import Any, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
from rich.table import Table
import structlog

from .config_manager import ConfigManager, AgentConfig

logger = structlog.get_logger(__name__)
console = Console()


class CLIInterface:
    """Interactive CLI for agent configuration."""

    def __init__(self, config_manager: ConfigManager):
        """Initialize CLI interface.

        Args:
            config_manager: Configuration manager instance.
        """
        self.config_manager = config_manager

    def show_main_menu(self) -> None:
        """Display and handle main configuration menu."""
        while True:
            console.clear()
            console.print(Panel.fit(
                "[bold blue]Marketplace Automation Agent[/bold blue]\n"
                "[dim]Configuration Manager[/dim]",
                border_style="blue",
            ))

            config = self.config_manager.config

            if config:
                console.print(f"\n[green]Current Mode:[/green] {config.mode}")
                console.print(f"[green]Marketplace:[/green] {config.marketplace.name}")
            else:
                console.print("\n[yellow]No configuration loaded[/yellow]")

            console.print("\n[bold]Main Menu[/bold]")
            console.print("1. View Current Configuration")
            console.print("2. Edit Budget Settings")
            console.print("3. Edit Filter Settings")
            console.print("4. Edit Scoring Settings")
            console.print("5. Edit Target Categories/Queries")
            console.print("6. Switch Mode (DRY_RUN/LIVE)")
            console.print("7. Save Configuration")
            console.print("8. Validate for LIVE Mode")
            console.print("9. Exit")

            choice = Prompt.ask("\nSelect option", choices=["1", "2", "3", "4", "5", "6", "7", "8", "9"])

            if choice == "1":
                self._show_config()
            elif choice == "2":
                self._edit_budget()
            elif choice == "3":
                self._edit_filter()
            elif choice == "4":
                self._edit_scoring()
            elif choice == "5":
                self._edit_targets()
            elif choice == "6":
                self._switch_mode()
            elif choice == "7":
                self._save_config()
            elif choice == "8":
                self._validate_live()
            elif choice == "9":
                break

    def _show_config(self) -> None:
        """Display current configuration."""
        console.clear()
        config = self.config_manager.config

        if not config:
            console.print("[red]No configuration loaded[/red]")
            Prompt.ask("Press Enter to continue")
            return

        console.print(Panel(
            self.config_manager.get_summary(),
            title="Current Configuration",
            border_style="green",
        ))

        Prompt.ask("\nPress Enter to continue")

    def _edit_budget(self) -> None:
        """Edit budget settings."""
        console.clear()
        config = self.config_manager.config

        if not config:
            console.print("[red]No configuration loaded[/red]")
            return

        console.print(Panel.fit("[bold]Budget Settings[/bold]", border_style="blue"))

        table = Table(show_header=True)
        table.add_column("Setting", style="cyan")
        table.add_column("Current Value", style="green")
        table.add_row("Daily Budget", f"${config.bidding.daily_budget:.2f}")
        table.add_row("Weekly Budget", f"${config.bidding.weekly_budget:.2f}")
        table.add_row("Per-Item Max", f"${config.bidding.per_item_max:.2f}")
        table.add_row("Max Daily Exposure", f"${config.bidding.max_daily_exposure:.2f}")
        table.add_row("Max Pending Bids", str(config.bidding.max_pending_bids))
        console.print(table)

        if Confirm.ask("\nEdit daily budget?", default=False):
            value = FloatPrompt.ask("Daily budget ($)", default=config.bidding.daily_budget)
            self.config_manager.update({"bidding": {"daily_budget": value}})

        if Confirm.ask("Edit weekly budget?", default=False):
            value = FloatPrompt.ask("Weekly budget ($)", default=config.bidding.weekly_budget)
            self.config_manager.update({"bidding": {"weekly_budget": value}})

        if Confirm.ask("Edit per-item max?", default=False):
            value = FloatPrompt.ask("Per-item max ($)", default=config.bidding.per_item_max)
            self.config_manager.update({"bidding": {"per_item_max": value}})

        if Confirm.ask("Edit max daily exposure?", default=False):
            value = FloatPrompt.ask("Max daily exposure ($)", default=config.bidding.max_daily_exposure)
            self.config_manager.update({"bidding": {"max_daily_exposure": value}})

        if Confirm.ask("Edit max pending bids?", default=False):
            value = IntPrompt.ask("Max pending bids", default=config.bidding.max_pending_bids)
            self.config_manager.update({"bidding": {"max_pending_bids": value}})

        console.print("[green]Budget settings updated![/green]")
        Prompt.ask("Press Enter to continue")

    def _edit_filter(self) -> None:
        """Edit filter settings."""
        console.clear()
        config = self.config_manager.config

        if not config:
            console.print("[red]No configuration loaded[/red]")
            return

        console.print(Panel.fit("[bold]Filter Settings[/bold]", border_style="blue"))

        table = Table(show_header=True)
        table.add_column("Setting", style="cyan")
        table.add_column("Current Value", style="green")
        table.add_row("Min Seller Rating", f"{config.filter.min_seller_rating}%")
        table.add_row("Min Seller Feedback", str(config.filter.min_seller_feedback))
        table.add_row("Min Price", f"${config.filter.min_price:.2f}")
        table.add_row("Max Price", f"${config.filter.max_price:.2f}")
        table.add_row("Require Images", str(config.filter.require_images))
        table.add_row("Max Bid Count", str(config.filter.max_bid_count))
        console.print(table)

        if Confirm.ask("\nEdit min seller rating?", default=False):
            value = FloatPrompt.ask("Min seller rating (%)", default=config.filter.min_seller_rating)
            self.config_manager.update({"filter": {"min_seller_rating": value}})

        if Confirm.ask("Edit min seller feedback?", default=False):
            value = IntPrompt.ask("Min seller feedback count", default=config.filter.min_seller_feedback)
            self.config_manager.update({"filter": {"min_seller_feedback": value}})

        if Confirm.ask("Edit price range?", default=False):
            min_val = FloatPrompt.ask("Min price ($)", default=config.filter.min_price)
            max_val = FloatPrompt.ask("Max price ($)", default=config.filter.max_price)
            self.config_manager.update({"filter": {"min_price": min_val, "max_price": max_val}})

        if Confirm.ask("Toggle require images?", default=False):
            value = not config.filter.require_images
            self.config_manager.update({"filter": {"require_images": value}})
            console.print(f"[green]Require images set to: {value}[/green]")

        if Confirm.ask("Edit max bid count filter?", default=False):
            value = IntPrompt.ask("Max bid count", default=config.filter.max_bid_count)
            self.config_manager.update({"filter": {"max_bid_count": value}})

        console.print("[green]Filter settings updated![/green]")
        Prompt.ask("Press Enter to continue")

    def _edit_scoring(self) -> None:
        """Edit scoring settings."""
        console.clear()
        config = self.config_manager.config

        if not config:
            console.print("[red]No configuration loaded[/red]")
            return

        console.print(Panel.fit("[bold]Scoring Settings[/bold]", border_style="blue"))

        table = Table(show_header=True)
        table.add_column("Setting", style="cyan")
        table.add_column("Current Value", style="green")
        table.add_row("Min Eligible Score", str(config.scoring.min_eligible_score))
        table.add_row("Seller Trust Weight", f"{config.scoring.weight_seller_trust:.2f}")
        table.add_row("Price Value Weight", f"{config.scoring.weight_price_value:.2f}")
        table.add_row("Condition Weight", f"{config.scoring.weight_condition:.2f}")
        table.add_row("Competition Weight", f"{config.scoring.weight_competition:.2f}")
        console.print(table)

        if Confirm.ask("\nEdit min eligible score?", default=False):
            value = FloatPrompt.ask("Min eligible score (0-100)", default=config.scoring.min_eligible_score)
            self.config_manager.update({"scoring": {"min_eligible_score": value}})

        console.print("[green]Scoring settings updated![/green]")
        Prompt.ask("Press Enter to continue")

    def _edit_targets(self) -> None:
        """Edit target categories and search queries."""
        console.clear()
        config = self.config_manager.config

        if not config:
            console.print("[red]No configuration loaded[/red]")
            return

        console.print(Panel.fit("[bold]Target Settings[/bold]", border_style="blue"))

        console.print("\n[cyan]Current Categories:[/cyan]")
        for cat in config.target_categories:
            console.print(f"  - {cat}")
        if not config.target_categories:
            console.print("  [dim]None[/dim]")

        console.print("\n[cyan]Current Search Queries:[/cyan]")
        for query in config.search_queries:
            console.print(f"  - {query}")
        if not config.search_queries:
            console.print("  [dim]None[/dim]")

        console.print(f"\n[cyan]Refresh Interval:[/cyan] {config.refresh_interval} seconds")

        if Confirm.ask("\nEdit categories?", default=False):
            cats = Prompt.ask("Enter categories (comma-separated)")
            categories = [c.strip() for c in cats.split(",") if c.strip()]
            self.config_manager.update({"target_categories": categories})

        if Confirm.ask("Edit search queries?", default=False):
            queries = Prompt.ask("Enter queries (comma-separated)")
            search_queries = [q.strip() for q in queries.split(",") if q.strip()]
            self.config_manager.update({"search_queries": search_queries})

        if Confirm.ask("Edit refresh interval?", default=False):
            value = IntPrompt.ask("Refresh interval (seconds)", default=config.refresh_interval)
            self.config_manager.update({"refresh_interval": value})

        console.print("[green]Target settings updated![/green]")
        Prompt.ask("Press Enter to continue")

    def _switch_mode(self) -> None:
        """Switch between DRY_RUN and LIVE modes."""
        console.clear()
        config = self.config_manager.config

        if not config:
            console.print("[red]No configuration loaded[/red]")
            return

        current = config.mode
        new_mode = "LIVE" if current == "DRY_RUN" else "DRY_RUN"

        console.print(f"Current mode: [bold]{current}[/bold]")
        console.print(f"Switch to: [bold]{new_mode}[/bold]")

        if new_mode == "LIVE":
            console.print("\n[yellow]WARNING: LIVE mode will execute real bids![/yellow]")
            console.print("Ensure you have validated your configuration.")

            is_valid, issues = self.config_manager.validate_for_live()
            if not is_valid:
                console.print("\n[red]Configuration issues for LIVE mode:[/red]")
                for issue in issues:
                    console.print(f"  - {issue}")

        if Confirm.ask(f"\nSwitch to {new_mode}?", default=False):
            self.config_manager.update({"mode": new_mode})
            console.print(f"[green]Mode switched to {new_mode}[/green]")
        else:
            console.print("Mode unchanged")

        Prompt.ask("Press Enter to continue")

    def _save_config(self) -> None:
        """Save current configuration."""
        console.clear()

        path = Prompt.ask(
            "Save path",
            default=str(self.config_manager.config_path or "config/config.yaml"),
        )

        try:
            self.config_manager.save(path=Path(path))
            console.print(f"[green]Configuration saved to {path}[/green]")
        except Exception as e:
            console.print(f"[red]Failed to save: {e}[/red]")

        Prompt.ask("Press Enter to continue")

    def _validate_live(self) -> None:
        """Validate configuration for LIVE mode."""
        console.clear()

        is_valid, issues = self.config_manager.validate_for_live()

        if is_valid:
            console.print(Panel(
                "[green]Configuration is valid for LIVE mode![/green]",
                border_style="green",
            ))
        else:
            console.print(Panel(
                "[red]Configuration has issues for LIVE mode[/red]",
                border_style="red",
            ))
            for issue in issues:
                console.print(f"  [red]x[/red] {issue}")

        Prompt.ask("\nPress Enter to continue")


# Click CLI commands
@click.group()
def cli():
    """Marketplace Automation Agent Configuration CLI."""
    pass


@cli.command()
@click.option("--config", "-c", type=click.Path(exists=True), help="Config file path")
def interactive(config: Optional[str]):
    """Launch interactive configuration menu."""
    config_manager = ConfigManager(Path(config) if config else None)

    try:
        config_manager.load()
    except FileNotFoundError:
        console.print("[yellow]No configuration found. Creating default...[/yellow]")

    interface = CLIInterface(config_manager)
    interface.show_main_menu()


@cli.command()
@click.option("--config", "-c", type=click.Path(exists=True), help="Config file path")
def show(config: Optional[str]):
    """Show current configuration."""
    config_manager = ConfigManager(Path(config) if config else None)

    try:
        config_manager.load()
        console.print(config_manager.get_summary())
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")


@cli.command()
@click.option("--config", "-c", type=click.Path(exists=True), help="Config file path")
def validate(config: Optional[str]):
    """Validate configuration for LIVE mode."""
    config_manager = ConfigManager(Path(config) if config else None)

    try:
        config_manager.load()
        is_valid, issues = config_manager.validate_for_live()

        if is_valid:
            console.print("[green]Configuration is valid for LIVE mode[/green]")
            sys.exit(0)
        else:
            console.print("[red]Configuration issues:[/red]")
            for issue in issues:
                console.print(f"  - {issue}")
            sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    cli()
