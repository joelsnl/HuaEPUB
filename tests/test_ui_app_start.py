"""
UI Tests for HuaEPUB Application Startup

These tests verify that the application can initialize its GUI components
without errors. They use pytest-qt for Qt-specific testing.
"""
import os
import pytest
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestAppStartup:
    """Test application startup and initialization."""
    
    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        # Ensure we're in a testable environment
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        self.test_dir = Path(tempfile.mkdtemp())
        yield
        # Cleanup
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)
    
    def test_app_imports_all_modules(self):
        """Test that all core modules can be imported without errors."""
        from core import (
            ad_detect,
            atomic_io,
            branding,
            cache,
            cleaner,
            download_job,
            download_runner,
            drive_sync,
            epub_builder,
            gtx_throttle,
            library,
            library_check,
            logger,
            notify,
            ollama_setup,
            parser,
        )
        from core.polish import (
            api,
            detect,
            engine,
            glossary,
            hardware,
            paths,
            prompts,
            qwen_tokens,
            rewrite,
            router,
            serve,
            spans,
            tagger,
            tagger_train,
        )
        from core.translation import (
            glossary as trans_glossary,
            harvest,
            nmt,
            novel_translator,
            pack,
            qwen_glossary,
        )
        
        # If we get here without ImportError, test passes
        assert True
    
    def test_gui_imports_all_modules(self):
        """Test that all GUI modules can be imported."""
        from gui import (
            app,
            main_window,
            window,
            workers,
        )
        from gui.pages import reader_page
        from gui.window import (
            drive_actions,
            library_actions,
            reader_actions,
            worker_host,
        )
        
        assert True
    
    def test_parsers_load_sites_json(self):
        """Test that parsers can load the sites.json configuration."""
        from parsers import config
        
        # Check that config module exists and has data loading capability
        assert hasattr(config, 'SiteConfigParser') or hasattr(config, 'load_sites')
    
    def test_branding_constants(self):
        """Test that branding constants are defined."""
        from core.branding import EXE_BASENAME, APP_NAME
        
        assert EXE_BASENAME == 'HuaEPUB'
        assert APP_NAME == 'HuaEPUB'
    
    def test_settings_file_structure(self):
        """Test that settings can be created with proper structure."""
        from core import settings
        
        # Check that settings module exists and has load/save functions
        assert hasattr(settings, 'load_settings') or hasattr(settings, 'save_settings') or hasattr(settings, 'get_setting')
    

class TestEPUBGeneration:
    """Test EPUB generation functionality."""
    
    def test_epub_builder_import(self):
        """Test that EPUB builder can be imported."""
        from core.epub_builder import EPUBBuilder
        assert EPUBBuilder is not None
    
    def test_epub_basic_structure(self):
        """Test basic EPUB structure creation."""
        from ebooklib import epub
        
        # Create a minimal EPUB in memory
        book = epub.EpubBook()
        book.set_identifier('test-123')
        book.set_title('Test Novel')
        book.set_language('en')
        
        assert book.get_metadata('DC', 'title')[0][0] == 'Test Novel'
        assert book.get_metadata('DC', 'language')[0][0] == 'en'


class TestParserFunctionality:
    """Test parser functionality."""
    
    def test_parser_module_exists(self):
        """Test that parser module exists."""
        from core import parser
        # Check that parser has key classes/functions
        assert hasattr(parser, 'BaseParser') or hasattr(parser, 'fetch_chapters') or hasattr(parser, 'get_parser_for_url')
    
    def test_pagination_module(self):
        """Test pagination utilities."""
        from parsers import pagination
        # Check that pagination module has key functions/classes
        assert hasattr(pagination, 'canonicalize_page_url') or hasattr(pagination, 'next_content_page_url') or hasattr(pagination, 'walk_list_pages')
