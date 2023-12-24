$(function () {
    var networks = undefined;

    function showHideFormFields() {
        var security = $(this).find(':selected').attr('data-security');
        // start off with all fields hidden
        $('#identity-group').addClass('hidden');
        $('#passphrase-group').addClass('hidden');
        $('#hidden-ssid-group').addClass('hidden');
        $('#noaa-station-id-group').removeClass('hidden'); // Show NOAA Station ID field

        if (security === 'NONE') {
            return; // nothing to do
        }
        if (security === 'ENTERPRISE') {
            $('#identity-group').removeClass('hidden');
            $('#passphrase-group').removeClass('hidden');
            return;
        }
        if (security === 'HIDDEN') {
            $('#hidden-ssid-group').removeClass('hidden');
            // fall through
        }
        // otherwise security is HIDDEN, WEP, WPA, or WPA2 which need password
        $('#passphrase-group').removeClass('hidden');
        
        // Ensure the "Show Password" button is created and its event handler is attached
        createShowPasswordButton();
    }

    $('#ssid-select').change(showHideFormFields);

    $.get("/regcode", function(data){
        if(data.length !== 0){
            $('#regcode').val(data);
        } else { 
            $('.reg-row').hide(); // no reg code, so hide that part of the UI
	}
    });

    $.get("/networks", function(data){
        if(data.length === 0){
            $('.before-submit').hide();
            $('#no-networks-message').removeClass('hidden');
        } else {
            networks = JSON.parse(data);
            $.each(networks, function(i, val){
                $('#ssid-select').append(
                    $('<option>')
                        .text(val.ssid)
                        .attr('val', val.ssid)
                        .attr('data-security', val.security.toUpperCase())
                );
            });

            jQuery.proxy(showHideFormFields, $('#ssid-select'))();
        }
    });

    // Example NOAA Station IDs
    var exampleStationIDs = ["ABC123", "DEF456", "GHI789"];

    // Populate the dropdown with example Station IDs
    var noaaStationIdDropdown = $("#noaa-station-id");
    $.each(exampleStationIDs, function (index, value) {
        noaaStationIdDropdown.append($('<option>', {
            value: value,
            text: value
        }));
    });

    // Ensure the "Show Password" button is created and its event handler is attached
    createShowPasswordButton();

    $('#connect-form').submit(function(ev){
        $.post('/connect', $('#connect-form').serialize(), function(data){
            $('.before-submit').hide();
            $('#submit-message').removeClass('hidden');
        });
        ev.preventDefault();
    });

    function createShowPasswordButton() {
        // Move the toggle-password button below the password field and justify it to the right
        var passwordGroup = $("#passphrase-group");
        var passwordInput = $("#password-input");

        // Check if the toggle-password button already exists before creating it
        var togglePasswordButton = $("#toggle-password");
        if (!togglePasswordButton.length) {
            // Create the toggle-password button
            togglePasswordButton = $("<button>", {
                class: "btn btn-default pull-right",
                type: "button",
                id: "toggle-password",
                text: "Show Password"
            });

            // Append the toggle-password button inside the password-group div
            passwordGroup.append(togglePasswordButton);

            // Event handler for the toggle-password button
            togglePasswordButton.click(function () {
                var passwordFieldType = passwordInput.attr('type');

                // Toggle the password visibility
                if (passwordFieldType === 'password') {
                    passwordInput.attr('type', 'text');
                    togglePasswordButton.text('Hide Password');
                } else {
                    passwordInput.attr('type', 'password');
                    togglePasswordButton.text('Show Password');
                }
            });
        }
    }
});
