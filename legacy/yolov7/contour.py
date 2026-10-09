import cv2
import numpy as np

def analyze_image_contours(image_path):
    """
    Loads an image, finds contours, calculates their area, and displays the result.

    Args:
        image_path (str): The path to the input image file.
    """
    # 1. Load the Image
    image = cv2.imread(image_path)
    if image is None:
        print(f"Error: Could not load image from path: {image_path}")
        return

    # Create a copy to draw on, so the original is not modified
    image_with_contours = image.copy()

    # 2. Pre-processing: Convert to Grayscale and Blur
    # Grayscale conversion is necessary for thresholding
    gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Gaussian blur helps to reduce noise and get cleaner contours
    blurred_image = cv2.GaussianBlur(gray_image, (5, 5), 0)

    # 3. Thresholding to get a binary image
    # We use THRESH_BINARY_INV because we assume a lighter object on a darker background.
    # The object will become white (255) and the background black (0).
    # cv2.THRESH_OTSU automatically calculates the optimal threshold value.
    # If your object is darker than the background, use cv2.THRESH_BINARY instead.
    _, binary_mask = cv2.threshold(blurred_image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # 4. Find Contours
    # cv2.RETR_EXTERNAL finds only the outer contours of the objects.
    # cv2.CHAIN_APPROX_SIMPLE compresses horizontal, vertical, and diagonal segments
    # and leaves only their end points, which saves memory.
    contours, hierarchy = cv2.findContours(binary_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    print(f"Found {len(contours)} initial contours.")

    # 5. Analyze and Draw Each Contour
    # Filter out small contours that might be noise
    min_contour_area = 100 
    
    final_contours = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area > min_contour_area:
            final_contours.append(contour)

    print(f"Found {len(final_contours)} contours after filtering by area > {min_contour_area} pixels.")

    for i, contour in enumerate(final_contours):
        # Calculate the area of the contour
        area = cv2.contourArea(contour)

        # Draw the contour on our copy of the image
        # The '-1' means draw all contours in the list (we pass a list with one contour)
        # (0, 255, 0) is the color green in BGR format, 2 is the thickness
        cv2.drawContours(image_with_contours, [contour], -1, (0, 255, 0), 2)

        # Find the center of the contour to place the text
        M = cv2.moments(contour)
        if M["m00"] != 0:
            cX = int(M["m10"] / M["m00"])
            cY = int(M["m01"] / M["m00"])
        else:
            # If moment is 0, use the top-left point of the contour
            cX, cY = contour[0, 0]

        # Put the text with the area value near the center of the contour
        text = f"Area: {int(area)}"
        cv2.putText(image_with_contours, text, (cX - 50, cY), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        print(f"  - Contour #{i+1}, Area: {int(area)} pixels")


    # 6. Display the results
    cv2.imshow('Original Image', image)
    cv2.imshow('Binary Mask', binary_mask)
    cv2.imshow('Image with Contours and Area', image_with_contours)

    # Wait for a key press to close the windows
    cv2.waitKey(0)
    cv2.destroyAllWindows()


if __name__ == '__main__':
    # --- IMPORTANT ---
    # Replace this with the path to YOUR image file.
    # Use an image with a relatively clean background for best results.
    image_file_path = '/home/aml023/Desktop/shashi/mehandi/out/twig/heena6_12_09_37__crop45_conf0.60.jpg' 
    
    # Using the leaf image from the previous question as an example.
    # You should replace 'leaf.jpg' with your file's name.
    # image_file_path = 'leaf.jpg' 
    
    analyze_image_contours(image_file_path)