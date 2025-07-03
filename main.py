from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from playwright.async_api import async_playwright, Page
import asyncio
import json
import subprocess
import time
import os
from urllib.parse import quote, urlparse, parse_qs, urlencode
import logging
from datetime import datetime
import requests
import sys
import platform

# Configure logging
log_filename = f"zepto_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI()

class OrderRequest(BaseModel):
    products: List[str]
    upi_id: Optional[str] = "your.upi@provider"  # Default UPI ID if not provided

async def handle_popups(page: Page):
    """Handle any popups by pressing escape and clicking close buttons."""
    try:
        # Check for common popup indicators
        popup_selectors = [
            # Super Saver specific selectors
            'div[style*="cart_supersaver_prominent_nudge_bg.png"] button',  # Super saver close button
            'button:has(svg path[stroke="#fff"])',  # Close button with white X icon
            'button:has-text("✕")',  # Close button with × symbol
            
            # General popup selectors
            'div[role="dialog"]',  # Common dialog/modal
            '.modal',  # Common modal class
            '[class*="popup"]',  # Any element with popup in class
            '[class*="modal"]',  # Any element with modal in class
            '.Super-Saver',  # Super saver popup
            '[class*="super-saver"]'  # Super saver related elements
        ]
        
        for selector in popup_selectors:
            try:
                popup = await page.wait_for_selector(selector, timeout=2000)
                if popup:
                    logger.info(f"Found popup with selector: {selector}")
                    # Try clicking the close button first
                    try:
                        await popup.click()
                        logger.info("Clicked popup close button")
                    except Exception:
                        # If clicking fails, try pressing escape
                        await page.keyboard.press('Escape')
                        logger.info("Pressed escape to close popup")
                    
                    await page.wait_for_timeout(500)  # Wait for popup animation
                    
                    # Verify if popup was closed
                    try:
                        is_visible = await popup.is_visible()
                        if is_visible:
                            logger.warning("Popup might still be visible after closing attempt")
                    except Exception:
                        logger.info("Popup appears to be closed")
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Error handling popups: {e}")

async def find_add_to_cart_button(page):
    """Try different selectors to find the Add to Cart button."""
    # selectors = [
    #     'button[data-testid$="-add-btn"]',  # Using data-testid attribute
    #     'button.border-skin-primary:has-text("Add to Cart")',  # Using class and text
    #     'button:has-text("Add to Cart")',  # Generic text matcher
    #     'button:has(span:text("Add to Cart"))'  # Looking for span inside button
    # ]
    selectors = [
        'data-testid="product-card"',
        'button[aria-label="add"]'
    ]
    
    for selector in selectors:
        try:
            button = await page.wait_for_selector(selector, timeout=2000)
            if button:
                logger.info(f"Found Add to Cart button using selector: {selector}")
                return button
        except Exception:
            continue
    
    return None

async def open_cart(page: Page):
    """Open cart using URL parameter."""
    try:
        current_url = page.url
        parsed_url = urlparse(current_url)
        params = parse_qs(parsed_url.query)
        params['cart'] = ['open']
        
        # Reconstruct URL with cart parameter
        new_query = urlencode(params, doseq=True)
        cart_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}?{new_query}"
        
        logger.info(f"Opening cart using URL: {cart_url}")
        await page.goto(cart_url, wait_until='networkidle')
        await page.wait_for_timeout(2000)  # Wait for cart to fully load
        return True
    except Exception as e:
        logger.error(f"Error opening cart: {e}")
        return False

def is_chrome_running():
    """Check if Chrome is running with remote debugging."""
    try:
        result = subprocess.run(
            ['curl', 'http://localhost:9222/json'],
            capture_output=True,
            text=True
        )
        return result.returncode == 0 and 'webSocketDebuggerUrl' in result.stdout
    except Exception:
        return False

async def ensure_chrome_running():
    """Ensure Chrome is running with remote debugging enabled."""
    try:
        logger.info("Checking if Chrome is running with remote debugging")

        if is_chrome_running():
            logger.info("Chrome already running with remote debugging")
            return True

        system = platform.system()
        logger.info(f"Detected OS: {system}")

        if system == "Darwin":  # macOS
            chrome_path = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
            subprocess.run(['pkill', '-f', 'Google Chrome'])
            user_data_dir = '/tmp/chrome-dev-profile'
        elif system == "Windows":
            chrome_path = os.path.expandvars(r'C:\browsers\chromium-1140\chrome.exe')
            subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            #user_data_dir = os.path.join(os.environ['TEMP'], 'chrome-dev-profile')
            user_data_dir = os.path.expandvars(r'C:\chrome-dev-profile')
        elif system == "Linux":
            chrome_path = '/usr/bin/chromium' # or '/usr/bin/google-chrome' depending on your image
            subprocess.run(['pkill', '-f', 'chromium'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            user_data_dir = '/tmp/chrome-dev-profile'
        else:
            logger.error("Unsupported OS")
            return False

        logger.info("Starting Chrome with remote debugging")
        subprocess.Popen([
            chrome_path,
            '--headless',
            '--remote-debugging-port=9222',
            '--no-first-run',
            '--no-default-browser-check',
            f'--user-data-dir={user_data_dir}',
            'https://www.zeptonow.com'
        ])

        # Wait for Chrome to start
        max_retries = 10
        for i in range(max_retries):
            logger.info(f"Waiting for Chrome to be ready (attempt {i+1}/{max_retries})")
            time.sleep(2)
            if is_chrome_running():
                logger.info("Chrome started successfully")
                return True

        logger.error("Chrome failed to start after multiple attempts")
        return False

    except Exception as e:
        logger.error(f"Error ensuring Chrome is running: {e}")
        return False

async def enter_upi_and_pay(page: Page, upi_id: str):
    """Enter UPI ID and click Verify and Pay button."""
    try:
        # Wait for UPI input field
        logger.info("Waiting for UPI input field")
        upi_input = await page.wait_for_selector('input[testid="edt_vpa"]', timeout=5000)
        if not upi_input:
            raise Exception("UPI input field not found")
        
        # Clear existing value and type UPI ID
        await upi_input.click()
        await upi_input.fill(upi_id)
        logger.info(f"Entered UPI ID: {upi_id}")
        
        # Wait for a moment to ensure UPI ID is properly entered
        await page.wait_for_timeout(1000)
        
        # Look for Verify and Pay button using multiple selectors
        verify_button_selectors = [
            'div[testid="msg_text"]:has-text("Verify and Pay")',
            'article:has-text("Verify and Pay")',
            'div.textView:has-text("Verify and Pay")',
            'div[id="10000258"]'  # Direct ID if available
        ]
        
        verify_button = None
        for selector in verify_button_selectors:
            try:
                verify_button = await page.wait_for_selector(selector, timeout=2000)
                if verify_button:
                    logger.info(f"Found Verify and Pay button using selector: {selector}")
                    break
            except Exception:
                continue
        
        if verify_button:
            await verify_button.click()
            logger.info("Clicked Verify and Pay button")
            return True
        else:
            raise Exception("Verify and Pay button not found")
            
    except Exception as e:
        logger.error(f"Error in UPI payment process: {e}")
        return False

@app.post("/order")
async def create_order(order: OrderRequest):
    order_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    logger.info(f"Received new order {order_id} with products: {order.products}")

    # Ensure Chrome is running with retry logic
    max_retries = 3
    for attempt in range(max_retries):
        if await ensure_chrome_running():
            break
        logger.warning(f"Failed to ensure Chrome is running (attempt {attempt+1}/{max_retries})")
        if attempt == max_retries - 1:
            raise HTTPException(status_code=500, detail="Could not ensure Chrome is running")
        time.sleep(2)

    try:
        async with async_playwright() as p:
            logger.info("Initializing Playwright")
            
            # Add retry logic for connecting to Chrome
            max_connect_retries = 3
            browser = None
            
            for attempt in range(max_connect_retries):
                try:
                    logger.info(f"Connecting to Chrome (attempt {attempt+1}/{max_connect_retries})")
                    browser = await p.chromium.connect_over_cdp('http://localhost:9222')
                    logger.info("Connected to Chrome instance")
                    break
                except Exception as e:
                    logger.warning(f"Failed to connect to Chrome: {e}")
                    if attempt < max_connect_retries - 1:
                        time.sleep(2)
                        # Try to restart Chrome by killing and starting fresh
                        try:
                            subprocess.run(['pkill', '-f', 'Google Chrome'])
                            time.sleep(2)
                        except:
                            pass
                        await ensure_chrome_running()
                        continue
                    else:
                        raise HTTPException(status_code=500, 
                                         detail=f"Failed to connect to Chrome after {max_connect_retries} attempts")
            
            if not browser:
                raise HTTPException(status_code=500, detail="Failed to connect to browser")
            
            # Get the default context (first one)
            contexts = browser.contexts
            if not contexts:
                logger.info("No existing context found, creating new page in default context")
                page = await browser.new_page()
            else:
                logger.info("Using existing browser context")
                context = contexts[0]
                pages = context.pages
                if pages:
                    page = pages[0]
                    logger.info("Using existing page")
                else:
                    logger.info("Creating new page in existing context")
                    page = await context.new_page()
            
            # Search and add each product
            successful_products = []
            failed_products = []
            
            for product in order.products:
                try:
                    logger.info(f"Processing product: {product}")
                    # Directly navigate to search URL
                    encoded_product = quote(product)
                    search_url = f'https://www.zeptonow.com/search?query={encoded_product}'
                    logger.info(f"Navigating to search URL: {search_url}")
                    await page.goto(search_url, wait_until='networkidle')
                    
                    # Handle any popups that might appear
                    await handle_popups(page)
                    
                    # Try to find and click the Add to Cart button
                    logger.info("Attempting to find Add to Cart button")
                    add_to_cart_button = await find_add_to_cart_button(page)
                    
                    if add_to_cart_button:
                        # Take screenshot before clicking (debug)
                        await page.screenshot(path=f'before_click_{order_id}_{product}.png')
                        
                        # Click the button
                        await add_to_cart_button.click()
                        await page.wait_for_timeout(1000)  # Wait for cart update
                        
                        # Handle any popups that might appear after adding to cart
                        await handle_popups(page)
                        
                        # Take screenshot after clicking (debug)
                        await page.screenshot(path=f'after_click_{order_id}_{product}.png')
                        
                        logger.info(f"Successfully added {product} to cart")
                        successful_products.append(product)

                        # Wait for and click the payment button
                        logger.info("Looking for payment button")
                        payment_button = await page.wait_for_selector('button:has-text("Click to Pay")', timeout=10000)
                        if payment_button:
                            await payment_button.click()
                            logger.info("Clicked payment button")
                        else:
                            raise HTTPException(status_code=400, detail="Payment button not found")
                        
                        await page.wait_for_timeout(1000)
                        
                        # Handle any popups before payment options
                        await handle_popups(page)
                        
                        # Wait for the payment options to load
                        logger.info("Waiting for payment options")
                        await page.wait_for_selector('text=UPI', timeout=10000)
                        
                        # Select UPI payment method
                        await page.click('text=UPI')
                        await page.wait_for_timeout(1000)
                        logger.info("Selected UPI payment method")
                        sys.exit()
                        # Enter UPI ID and click Verify and Pay
                        if not await enter_upi_and_pay(page, order.upi_id):
                            raise HTTPException(status_code=400, detail="Failed to process UPI payment")
                        
                        # Wait for a moment to ensure the payment process started
                        await page.wait_for_timeout(2000)
                        
                        # Capture screenshot for verification
                        screenshot_path = f'order_status_{order_id}.png'
                        await page.screenshot(path=screenshot_path)
                        logger.info(f"Captured order screenshot: {screenshot_path}")
                        
                        logger.info(f"Order {order_id} completed successfully")

                        return {
                            "status": "success",
                            "order_id": order_id,
                            "message": "Order process completed and payment initiated",
                            "products_added": successful_products,
                            "products_failed": failed_products,
                            "upi_id_used": order.upi_id,
                            "screenshot": screenshot_path
                        }
                    else:
                        logger.warning(f"Product not found or Add to Cart button not visible: {product}")
                        failed_products.append(product)
                        raise HTTPException(status_code=404, detail=f"Product {product} not found or Add to Cart button not visible")
                except Exception as e:
                    logger.error(f"Error processing product {product}: {e}")
                    failed_products.append(product)
                    raise HTTPException(status_code=400, detail=f"Error adding product {product}: {str(e)}")
            
            try:
                logger.info("Proceeding to checkout")
                # Handle any popups before opening cart
                await handle_popups(page)
                
                # Open cart using URL parameter
                if not await open_cart(page):
                    raise HTTPException(status_code=400, detail="Failed to open cart")
                
                # Handle any popups that might appear after opening cart
                await handle_popups(page)
                
                logger.info("Successfully opened cart")
                
                return page, order_id, successful_products, failed_products
            except Exception as e:
                logger.error(f"Error during checkout: {e}")
                raise HTTPException(status_code=400, detail=f"Error during checkout: {str(e)}")
            
    except Exception as e:
        logger.error(f"Browser automation error: {e}")
        raise HTTPException(status_code=500, detail=f"Browser automation error: {str(e)}")




if __name__ == "__main__":
     logger.info("Starting Zepto Order Automation Server")
     import uvicorn
     uvicorn.run(app, host="0.0.0.0", port=8000)






