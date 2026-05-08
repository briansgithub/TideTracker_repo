$(function(){
    var networks = undefined;

    function showHideFormFields() {
        var security = $(this).find(':selected').attr('data-security');
        // start off with all fields hidden
        $('#identity-group').addClass('hidden');
        $('#passphrase-group').addClass('hidden');
        $('#hidden-ssid-group').addClass('hidden');
        if(security === 'NONE') {
            updateWifiSubmitButton();
            return; // nothing to do
        }
        if(security === 'ENTERPRISE') {
            $('#identity-group').removeClass('hidden');
            $('#passphrase-group').removeClass('hidden');
            
            // Uncheck the no-password checkbox when switching networks
            $('#no-password-checkbox').prop('checked', false);
            $('#passphrase').prop('disabled', false);
            $('#passphrase').show();
            $('#showPasswordBtn').show();
            
            updateWifiSubmitButton();
            return;
        } 
        if(security === 'HIDDEN') {
            $('#hidden-ssid-group').removeClass('hidden');
            // fall through
        } 
        // otherwise security is HIDDEN, WEP, WPA, or WPA2 which need password
        $('#passphrase-group').removeClass('hidden');
        
        // Uncheck the no-password checkbox when switching networks
        $('#no-password-checkbox').prop('checked', false);
        $('#passphrase').prop('disabled', false);
        $('#passphrase').show();
        $('#showPasswordBtn').show();
        
        updateWifiSubmitButton();
    }

    function updateWifiSubmitButton() {
        var isPasswordGroupHidden = $('#passphrase-group').hasClass('hidden');
        var isNoPasswordChecked = $('#no-password-checkbox').is(':checked');
        var passwordValue = $('#passphrase').val().trim();
        
        if (isPasswordGroupHidden) {
            $('#wifiSubmitBtn').prop('disabled', false);
        } else {
            if (isNoPasswordChecked || passwordValue.length > 0) {
                $('#wifiSubmitBtn').prop('disabled', false);
            } else {
                $('#wifiSubmitBtn').prop('disabled', true);
            }
        }
    }

    $('#no-password-checkbox').change(function() {
        if ($(this).is(':checked')) {
            $('#passphrase').val('');
            $('#passphrase').hide();
            $('#showPasswordBtn').hide();
        } else {
            $('#passphrase').show();
            $('#showPasswordBtn').show();
        }
        updateWifiSubmitButton();
    });

    $('#passphrase').on('input', function() {
        updateWifiSubmitButton();
    });



    // Add an event listener to update the input field when the dropdown changes
    $('#noaa-station-dropdown').change(function () {
        var selectedOption = this.options[this.selectedIndex];
        $('#noaa-station').val(selectedOption.text);
    });



    // Fetch networks and populate dropdown
    $.get("/networks", function(data) {
        if(data.length === 0) {
            $('.before-submit').hide();
            $('#no-networks-message').removeClass('hidden');
        } else {
            networks = JSON.parse(data);
            $.each(networks, function(i, val) {
                $('#ssid-select').append(
                    $('<option>')
                        .text(val.ssid)
                        .attr('val', val.ssid)
                        .attr('data-security', val.security.toUpperCase())
                );
            });
            showHideFormFields.call($('#ssid-select'));
        }
    });

    $('#ssid-select').change(showHideFormFields);

    // Function to toggle password visibility
    $('#showPasswordBtn').click(function() {
        var passwordField = $('#passphrase');
        var passwordType = passwordField.attr('type');
        passwordField.attr('type', passwordType === 'password' ? 'text' : 'password');
    });

    // Fetch current status on load
    $.get("/status", function(data) {
        var ssid = (data.ssid) ? data.ssid : 'None';
        $('#wifi-status-ssid').text('Currently connected to: ' + ssid);
        if (data.internet) {
            $('#wifi-status-internet').text('Has internet access').css('color', 'green');
        } else {
            $('#wifi-status-internet').text('No internet access').css('color', 'red');
        }
    });

    // Handle WiFi form submission
    $('#connect-form').submit(function(e) {
        e.preventDefault();
        $('#wifi-confirm-msg').hide();
        $('#wifi-status-internet').text('Checking...').css('color', 'orange');
        
        $.post('/connect', $(this).serialize(), function() {
            $('#wifi-confirm-msg').fadeIn();
            setTimeout(function() {
                $('#wifi-status-internet').text('Please refresh page').css('color', 'orange');
            }, 10000);
            setTimeout(function(){ $('#wifi-confirm-msg').fadeOut(); }, 5000);
        });
    });

    // Handle Station form submission
    $('#station-form').submit(function(e) {
        e.preventDefault();
        $.post('/update_station', $(this).serialize(), function() {
            $('#station-confirm-msg').fadeIn();
            setTimeout(function(){ $('#station-confirm-msg').fadeOut(); }, 5000);
        });
    });

    // Handle Exit Setup
    $('#exitBtn').click(function() {
        if(confirm("The Pi will now attempt to connect to WiFi and the hotspot will close. Proceed?")) {
            $.post('/exit');
            $('body').html('<div style="padding:20px; text-align:center;"><h1>Connecting...</h1><p>The hotspot is closing. Please wait for the Pi to join your WiFi network.</p></div>');
        }
    });

    $.get("/regcode", function(data) {
        if(data) $('#regcode').val(data);
    });
});